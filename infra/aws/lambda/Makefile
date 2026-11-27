# SAM makefile build for SyncBotFunction.
# Copies syncbot/ so `import app` works, installs pins, downloads Litestream.
#
# Build with `sam build --build-in-source`. syncbot/ sits above CodeUri, and SAM
# neither mounts it into a build container nor copies it into the build scratch
# directory, so any other mode leaves this recipe with nothing to copy.
LITESTREAM_VERSION := v0.3.13
LITESTREAM_SHA256 := eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0
LAMBDA_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
SYNCBOT_SRC := $(LAMBDA_DIR)/../../../syncbot
LITESTREAM_URL := https://github.com/benbjohnson/litestream/releases/download/$(LITESTREAM_VERSION)/litestream-$(LITESTREAM_VERSION)-linux-amd64.tar.gz

# The build runs on the host rather than in a Lambda container, so the wheel
# target is pinned to the runtime (CPython 3.12, Amazon Linux 2023, x86_64).
# Without this a macOS or arm64 host silently produces unimportable binaries.
PIP_TARGET_FLAGS := --only-binary=:all: --implementation cp --python-version 3.12 \
  --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64
# pip evaluates environment markers against the build host, not the target, so
# they are stripped: every pin in the export applies to the runtime above.
LAMBDA_REQUIREMENTS := /tmp/syncbot-lambda-requirements.txt
SHA256_CHECK := $(shell command -v sha256sum >/dev/null 2>&1 && echo 'sha256sum -c -' || echo 'shasum -a 256 -c -')

# Lambda cannot write under /var/task, so a module without usable bytecode is
# compiled from source on every cold start. `sam deploy` zips every file with a
# 1980 timestamp, so a timestamp-checked .pyc never matches its source. Ship
# CPython 3.12 bytecode that loads without that check; the deployed package
# never changes in place. A build host without Python 3.12 ships no bytecode.
PYC_PYTHON := $(shell for p in python3.12 python3; do $$p -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null && { echo $$p; break; }; done)

build-SyncBotFunction:
	cp -a "$(SYNCBOT_SRC)/." "$(ARTIFACTS_DIR)/"
	sed 's/ ;.*$$//' "$(SYNCBOT_SRC)/requirements.txt" > "$(LAMBDA_REQUIREMENTS)"
	python3 -m pip install -r "$(LAMBDA_REQUIREMENTS)" -t "$(ARTIFACTS_DIR)" $(PIP_TARGET_FLAGS) --no-compile --quiet
	rm -f "$(LAMBDA_REQUIREMENTS)"
	cp "$(LAMBDA_DIR)/handler.py" "$(ARTIFACTS_DIR)/"
	cp "$(LAMBDA_DIR)/litestream.yml" "$(ARTIFACTS_DIR)/"
	curl -sL -o /tmp/litestream.tar.gz "$(LITESTREAM_URL)"
	echo "$(LITESTREAM_SHA256)  /tmp/litestream.tar.gz" | $(SHA256_CHECK)
	tar -xzf /tmp/litestream.tar.gz -C "$(ARTIFACTS_DIR)"
	chmod +x "$(ARTIFACTS_DIR)/litestream"
	rm -f /tmp/litestream.tar.gz
	rm -rf "$(ARTIFACTS_DIR)/bin"
	find "$(ARTIFACTS_DIR)" -type d -name __pycache__ -prune -exec rm -rf {} +
	@if [ -n "$(PYC_PYTHON)" ]; then \
	  $(PYC_PYTHON) -m compileall -q -j 0 --invalidation-mode unchecked-hash "$(ARTIFACTS_DIR)"; \
	else \
	  echo "warning: no Python 3.12 on PATH, so Lambda compiles every module on each cold start" >&2; \
	fi
