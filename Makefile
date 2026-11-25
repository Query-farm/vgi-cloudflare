# vgi-cloudflare — test + deploy orchestration.
#
#   make test-unit    # pytest (mocked HTTP, no network)
#   make test-sql     # SQL sqllogictest (test/sql + test/mock) over stdio, launch and http
#   make test-sql TRANSPORTS=launch   # ...over the listed transports only
#   make test-cloud   # SQL sqllogictest against deployed Fly.io service
#   make test         # unit + SQL
#   make codegen      # regenerate resources_generated.json from the OpenAPI spec
#   make update-api   # fetch/pin upstream spec, regenerate, write a compatibility report
#   make deploy       # build, smoke-test, push, deploy to Fly.io

# SQL tests run test/sql/*.test and test/mock/*.test (against tests/mock_cloudflare.py)
# through the published haybarn-unittest with the signed community vgi extension
# (ci/run-integration.sh; no local DuckDB/vgi builds).
HAYBARN_VERSION ?= 2.0.1rc3
WORKER         := $(CURDIR)/.venv/bin/vgi-cloudflare
TOOLS          := $(CURDIR)/.tools
HAYBARN_UNITTEST := $(TOOLS)/bin/haybarn-unittest
TRANSPORTS     ?= stdio launch http
WORKER_CLOUD   ?= https://$(FLY_APP).fly.dev
# Allowed skips: the live tests without a token.
EXPECTED_SKIP_REASONS ?= require-env CLOUDFLARE_API_TOKEN

FLY_APP        ?= vgi-cloudflare

# Live tests (test/sql/live*.test) read a Cloudflare API token from the environment,
# falling back to this file. Exported, never echoed in a recipe.
CLOUDFLARE_API_TOKEN_FILE ?= $(HOME)/cf-read-token.txt
ifeq ($(origin CLOUDFLARE_API_TOKEN),undefined)
CLOUDFLARE_API_TOKEN := $(strip $(shell cat "$(CLOUDFLARE_API_TOKEN_FILE)" 2>/dev/null))
endif
ifneq ($(CLOUDFLARE_API_TOKEN),)
export CLOUDFLARE_API_TOKEN
endif

.PHONY: test test-unit test-sql tools test-cloud codegen update-api build smoke-test push deploy lint

test: test-unit test-sql

test-unit:
	.venv/bin/pytest tests/ --rootdir=. -o "addopts=" -q

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

codegen:
	.venv/bin/python tools/generate_resources.py --emit

update-api:
	.venv/bin/python -m tools.update_api

tools:
	@test -x $(HAYBARN_UNITTEST) || { uv venv -q $(TOOLS) && uv pip install -q -p $(TOOLS) haybarn-unittest==$(HAYBARN_VERSION); }

# With a token, the live analytics tests get a zone/account it can see and a
# one-day window.
LIVE_TEST_ENV = IDS="$$(.venv/bin/python tools/live_test_ids.py)"; \
	export CLOUDFLARE_TEST_ZONE_ID="$${IDS%% *}" CLOUDFLARE_TEST_ACCOUNT_ID="$${IDS\#\#* }"; \
	export CLOUDFLARE_TEST_SINCE="$$(.venv/bin/python -c 'import datetime as d; print(d.date.today() - d.timedelta(days=1))')"; \
	export CLOUDFLARE_TEST_UNTIL="$$(.venv/bin/python -c 'import datetime as d; print(d.date.today())')"

test-sql: tools
	@test -x $(WORKER) || uv sync
	@$(LIVE_TEST_ENV); \
	for t in $(TRANSPORTS); do \
		WORKER=$(WORKER) HAYBARN_UNITTEST=$(HAYBARN_UNITTEST) TRANSPORT=$$t PYTHON=$(CURDIR)/.venv/bin/python \
			EXPECTED_SKIP_REASONS="$(EXPECTED_SKIP_REASONS)" ci/run-integration.sh || exit 1; \
	done

test-cloud: tools
	@$(LIVE_TEST_ENV); \
	REMOTE_URL="$(WORKER_CLOUD)" HAYBARN_UNITTEST=$(HAYBARN_UNITTEST) TRANSPORT=remote \
		EXPECTED_SKIP_REASONS="$(EXPECTED_SKIP_REASONS)" ci/run-integration.sh

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
