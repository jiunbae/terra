.PHONY: dev production build test install-images

dev:
	./start.sh --dev

production:
	./start.sh --production

build:
	./scripts/build_frontend_atomic.sh

test:
	cd frontend && npm run lint && npm run build
	cd backend && uv run pytest -q

install-images:
	uv tool install mflux==0.18.0
