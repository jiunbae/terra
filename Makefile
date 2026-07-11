.PHONY: dev production build test install-images

dev:
	./start.sh --dev

production:
	./start.sh --production

build:
	cd frontend && npm run build

test:
	cd frontend && npm run build
	cd backend && uv run python -m unittest discover -s tests -v

install-images:
	uv tool install --upgrade mflux
