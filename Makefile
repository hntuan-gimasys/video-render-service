.PHONY: install dev test build run-docker deploy fmt

install:
	python -m venv .venv && .venv/bin/pip install -r requirements.txt

dev:
	.venv/bin/uvicorn app.main:app --reload --port 8080

test:
	.venv/bin/pytest -q

build:
	docker build -t video-render-service:local .

run-docker:
	docker run --rm -p 8080:8080 \
		-e API_KEY=devkey \
		-e GOOGLE_APPLICATION_CREDENTIALS=/secrets/sa.json \
		-v $$(pwd)/sa-key.json:/secrets/sa.json:ro \
		--memory=8g --shm-size=1g \
		video-render-service:local

deploy:
	bash scripts/deploy.sh

smoke:
	bash scripts/smoke_test.sh
