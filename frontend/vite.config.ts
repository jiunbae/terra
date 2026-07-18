import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8787',
      '/generated': 'http://127.0.0.1:8787',
    },
  },
  build: {
    // 프로덕션 재배포는 새 릴리스 디렉터리에 빌드한 뒤 dist 심링크를 원자적으로 재지정한다.
    outDir: process.env.TERRA_BUILD_OUT_DIR || 'dist',
    // three 코어는 지연 로드되는 벤더 청크라 크기가 커도 초기 로드에 영향이 없다.
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        // three는 크고 거의 바뀌지 않으므로 별도 청크로 고정해 재방문 캐시 적중률을 높인다.
        manualChunks: (id) =>
          id.includes('node_modules/three/') ? 'three' : undefined,
      },
    },
  },
})
