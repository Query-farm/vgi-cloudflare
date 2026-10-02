# vgi-cloudflare — test + deploy orchestration.
#
#   make test-unit    # pytest (mocked HTTP, no network)
#   make test-stdio   # SQL sqllogictest, worker as subprocess
#   make test-http    # SQL sqllogictest, local HTTP server
#   make test-cloud   # SQL sqllogictest against deployed Fly.io service
#   make test-mock    # SQL sqllogictest of lateral functions against a mock Cloudflare
#   make test         # unit + stdio + mock
#   make codegen      # regenerate resources_generated.json from the OpenAPI spec
#   make deploy       # build, smoke-test, push, deploy to Fly.io

# The sqllogictest runner from a build of the DuckDB `vgi` extension.
VGI_BUILD_DIR  ?= $(HOME)/Development/vgi/build/release
TEST_RUNNER     = $(VGI_BUILD_DIR)/test/unittest
TEST_DIR        = .
TEST_PATTERN    = test/sql/*

WORKER_STDIO   ?= uv run --python 3.13 cloudflare_worker.py
WORKER_HTTP    ?= http://localhost:8000
WORKER_CLOUD   ?= https://$(FLY_APP).fly.dev
HTTP_PORT      ?= 8000
MOCK_PORT      ?= 18787

FLY_APP        ?= vgi-cloudflare

# Live tests (test/sql/live.test) read a Cloudflare API token from the environment,
# falling back to this file. Exported, never echoed in a recipe.
CLOUDFLARE_API_TOKEN_FILE ?= $(HOME)/cf-read-token.txt
ifeq ($(origin CLOUDFLARE_API_TOKEN),undefined)
CLOUDFLARE_API_TOKEN := $(strip $(shell cat "$(CLOUDFLARE_API_TOKEN_FILE)" 2>/dev/null))
endif
ifneq ($(CLOUDFLARE_API_TOKEN),)
export CLOUDFLARE_API_TOKEN
endif

.PHONY: test test-unit test-stdio test-mock test-http test-cloud codegen build smoke-test push deploy lint

test: test-unit test-stdio test-mock

test-unit:
	.venv/bin/pytest tests/ --rootdir=. -o "addopts=" -q

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

codegen:
	.venv/bin/python tools/generate_resources.py --emit

test-stdio:
	@IDS="$$(.venv/bin/python tools/live_test_ids.py)"; \
		export CLOUDFLARE_TEST_ZONE_ID="$${IDS%% *}" CLOUDFLARE_TEST_ACCOUNT_ID="$${IDS##* }"; \
		VGI_CLOUDFLARE_WORKER="$(WORKER_STDIO)" $(TEST_RUNNER) --test-dir "$(TEST_DIR)" "$(TEST_PATTERN)"

# The vgi extension doesn't pass its environment to the worker, so the mock's
# base URL rides along in the worker command itself.
test-mock:
	@if lsof -iTCP:$(MOCK_PORT) -sTCP:LISTEN -t >/dev/null 2>&1; then \
		echo "ERROR: port $(MOCK_PORT) already in use" >&2; exit 1; \
	fi
	@.venv/bin/python tests/mock_cloudflare.py $(MOCK_PORT) & \
		MOCK_PID=$$!; \
		sleep 1; \
		VGI_CLOUDFLARE_MOCK_WORKER="env CLOUDFLARE_API_BASE=http://127.0.0.1:$(MOCK_PORT)/client/v4 $(WORKER_STDIO)" \
			$(TEST_RUNNER) --test-dir "$(TEST_DIR)" "test/mock/*"; \
		TEST_EXIT=$$?; \
		kill $$MOCK_PID 2>/dev/null; wait $$MOCK_PID 2>/dev/null; \
		exit $$TEST_EXIT

test-http:
	@if lsof -iTCP:$(HTTP_PORT) -sTCP:LISTEN -t >/dev/null 2>&1; then \
		echo "ERROR: port $(HTTP_PORT) already in use" >&2; exit 1; \
	fi
	@VGI_SIGNING_KEY=dev .venv/bin/python serve.py --port $(HTTP_PORT) & \
		SERVER_PID=$$!; \
		sleep 1; \
		VGI_CLOUDFLARE_WORKER="$(WORKER_HTTP)" $(TEST_RUNNER) --test-dir "$(TEST_DIR)" "$(TEST_PATTERN)"; \
		TEST_EXIT=$$?; \
		kill $$SERVER_PID 2>/dev/null; wait $$SERVER_PID 2>/dev/null; \
		exit $$TEST_EXIT

test-cloud:
	VGI_CLOUDFLARE_WORKER="$(WORKER_CLOUD)" $(TEST_RUNNER) --test-dir "$(TEST_DIR)" "$(TEST_PATTERN)"

GIT_COMMIT     := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
IMAGE_TAG      := $(GIT_COMMIT)-$(shell date +%Y%m%d%H%M%S)
IMAGE          := registry.fly.io/$(FLY_APP):$(IMAGE_TAG)

build:
	docker build --platform linux/amd64 --build-arg GIT_COMMIT=$(GIT_COMMIT) -t $(IMAGE) .

smoke-test: build
	@docker run --rm --platform linux/amd64 -e VGI_SIGNING_KEY=dev $(IMAGE) \
		python -c "from vgi_cloudflare.worker import CloudflareWorker; print('imports OK')"
	@CID=$$(docker run -d --platform linux/amd64 -e VGI_SIGNING_KEY=dev -p 18000:8000 $(IMAGE)); \
		trap "docker rm -f $$CID >/dev/null" EXIT; \
		for i in 1 2 3 4 5 6 7 8 9 10; do \
			if curl -fsS -o /dev/null -w "%{http_code}\n" http://localhost:18000/health 2>/dev/null | grep -qE '^(200|401|403|404)$$'; then \
				echo "HTTP server responding"; exit 0; \
			fi; sleep 1; \
		done; \
		echo "ERROR: container did not respond on /health within 10s" >&2; docker logs $$CID >&2; exit 1

push: smoke-test
	fly auth docker
	docker push $(IMAGE)

deploy: push
	fly deploy --image $(IMAGE) --app $(FLY_APP)
