import * as THREE from 'three'

/**
 * 카메라 중심 구면 geometry clipmap용 정규화 격자.
 * level 0은 완전한 정사각형, 바깥 level은 중앙을 비운 링으로 만든다.
 * 실제 구면 투영과 높이 변위는 vertex shader에서 수행한다.
 */
export function createClipmapGeometry(segments: number, innerRatio = 0): THREE.BufferGeometry {
  const side = segments + 1
  const positions = new Float32Array(side * side * 3)
  let cursor = 0
  for (let y = 0; y <= segments; y++) {
    const v = (y / segments) * 2 - 1
    for (let x = 0; x <= segments; x++) {
      const u = (x / segments) * 2 - 1
      positions[cursor++] = u
      positions[cursor++] = v
      positions[cursor++] = 0
    }
  }

  const indices: number[] = []
  for (let y = 0; y < segments; y++) {
    const cy = Math.abs(((y + 0.5) / segments) * 2 - 1)
    for (let x = 0; x < segments; x++) {
      const cx = Math.abs(((x + 0.5) / segments) * 2 - 1)
      if (innerRatio > 0 && Math.max(cx, cy) < innerRatio) continue
      const a = y * side + x
      const b = a + 1
      const c = a + side
      const d = c + 1
      indices.push(a, c, b, b, c, d)
    }
  }

  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geometry.setIndex(indices)
  // vertex shader가 단위구 근처로 옮기므로 CPU 측 평면 경계는 사용하지 않는다.
  geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 2)
  return geometry
}
