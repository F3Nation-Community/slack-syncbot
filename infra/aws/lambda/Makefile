# SAM makefile build for SyncBotFunction.
# Copies syncbot/ so `import app` works, installs pins, downloads Litestream.
LITESTREAM_VERSION := v0.3.13
LITESTREAM_SHA256 := eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0
SYNCBOT_SRC := $(CURDIR)/../../../syncbot
LITESTREAM_URL := https://github.com/benbjohnson/litestream/releases/download/$(LITESTREAM_VERSION)/litestream-$(LITESTREAM_VERSION)-linux-amd64.tar.gz

build-SyncBotFunction:
	cp -a "$(SYNCBOT_SRC)/." "$(ARTIFACTS_DIR)/"
	python -m pip install -r "$(SYNCBOT_SRC)/requirements.txt" -t "$(ARTIFACTS_DIR)" --quiet
	cp handler.py "$(ARTIFACTS_DIR)/"
	cp litestream.yml "$(ARTIFACTS_DIR)/"
	curl -sL -o /tmp/litestream.tar.gz "$(LITESTREAM_URL)"
	echo "$(LITESTREAM_SHA256)  /tmp/litestream.tar.gz" | sha256sum -c -
	tar -xzf /tmp/litestream.tar.gz -C "$(ARTIFACTS_DIR)"
	chmod +x "$(ARTIFACTS_DIR)/litestream"
	rm -f /tmp/litestream.tar.gz
	rm -rf "$(ARTIFACTS_DIR)/bin"
