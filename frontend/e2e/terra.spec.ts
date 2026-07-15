import { expect, test } from '@playwright/test'
import { captureSevereBrowserErrors, installBrowserMocks } from './fixtures'

test('initial shell appears before the lazy 3D scene and reaches a canvas or graceful fallback', async ({ page }) => {
  test.setTimeout(60_000)
  const severeErrors = captureSevereBrowserErrors(page)
  const unexpectedApiRequests = await installBrowserMocks(page)
  let releaseScene!: () => void
  const sceneReleased = new Promise<void>((resolve) => { releaseScene = resolve })

  await page.route(/\/assets\/PlanetScene-[^/]+\.js$/, async (route) => {
    await sceneReleased
    await route.continue()
  })

  try {
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    await expect(page.getByRole('heading', { name: 'TERRA' })).toBeVisible()
    await expect(page.getByRole('complementary', { name: '행성 재구성 정보 패널' })).toBeVisible()
    await expect(page.getByText('3D 장면 불러오는 중…', { exact: true })).toBeVisible()
  } finally {
    releaseScene()
  }

  await expect.poll(async () => {
    if (await page.locator('.scene-container canvas').count()) return 'canvas'
    if (await page.getByRole('alert').filter({ hasText: '3D 장면을 표시할 수 없습니다.' }).count()) return 'fallback'
    return 'pending'
  }, { timeout: 20_000 }).not.toBe('pending')

  expect(unexpectedApiRequests).toEqual([])
  expect(severeErrors).toEqual([])
})

test('shared planet hydrates its report and every saved inhabitant asset', async ({ page }) => {
  const severeErrors = captureSevereBrowserErrors(page)
  const unexpectedApiRequests = await installBrowserMocks(page)

  await page.goto('/?planet=shared-world')

  await expect(page.getByRole('heading', { name: '아르카디아' })).toBeVisible()
  await expect(page.getByRole('button', { name: '분석 리포트' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('img', { name: '루멘 초상' })).toHaveAttribute('src', /inhabitant-lumen\.png$/)
  await expect(page.getByRole('img', { name: '모라 초상' })).toHaveAttribute('src', /inhabitant-mora\.png$/)
  await expect(page.getByRole('img', { name: '루멘 초상' })).toBeVisible()
  await expect(page.getByRole('img', { name: '모라 초상' })).toBeVisible()

  expect(unexpectedApiRequests).toEqual([])
  expect(severeErrors).toEqual([])
})

test('gallery navigation opens a mocked public planet without generation requests', async ({ page }) => {
  const severeErrors = captureSevereBrowserErrors(page)
  const unexpectedApiRequests = await installBrowserMocks(page)

  await page.goto('/')
  await page.getByRole('button', { name: '갤러리' }).click()
  await expect(page.getByRole('heading', { name: '행성 아카이브' })).toBeVisible()
  await expect(page.getByRole('button', { name: /아르카디아/ })).toContainText('거주민 2')
  await page.getByRole('button', { name: /아르카디아/ }).click()

  await expect(page.getByRole('heading', { name: '아르카디아' })).toBeVisible()
  await expect(page.getByRole('button', { name: '분석 리포트' })).toHaveAttribute('aria-pressed', 'true')

  expect(unexpectedApiRequests).toEqual([])
  expect(severeErrors).toEqual([])
})

test('scene error boundary preserves the application when the lazy 3D chunk fails', async ({ page }) => {
  const unexpectedApiRequests = await installBrowserMocks(page)
  await page.route(/\/assets\/PlanetScene-[^/]+\.js$/, (route) => route.fulfill({
    status: 200,
    contentType: 'application/javascript',
    body: 'throw new Error("intentional e2e scene failure")',
  }))

  await page.goto('/')

  const fallback = page.getByRole('alert').filter({ hasText: '3D 장면을 표시할 수 없습니다.' })
  await expect(fallback).toBeVisible()
  await expect(fallback).toContainText('분석 리포트와 생성 이미지는 계속 이용할 수 있습니다.')
  await expect(fallback.getByRole('button', { name: '3D 다시 불러오기' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'TERRA' })).toBeVisible()
  await expect(page.getByRole('button', { name: '갤러리' })).toBeEnabled()
  expect(unexpectedApiRequests).toEqual([])
})
