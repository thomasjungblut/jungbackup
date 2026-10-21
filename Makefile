APP_ID   := io.jungblut.JungBackup
MANIFEST := build-aux/flatpak/$(APP_ID).yml
BUNDLE   := jungbackup.flatpak
BUILDER  := flatpak run org.flatpak.Builder
LINT     := flatpak run --command=flatpak-builder-lint org.flatpak.Builder
SDK      := 51

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Development -------------------------------------------------------------

.venv:
	uv venv --python /usr/bin/python3 --system-site-packages
	uv sync

.PHONY: setup
setup: .venv ## Create the uv venv (uses the system's GTK bindings)

.PHONY: run
run: .venv ## Run the app from the source tree
	uv run jungbackup

.PHONY: lint
lint: .venv ## Lint Python code with ruff
	uv run ruff check src

.PHONY: validate
validate: ## Validate the desktop file and AppStream metainfo
	desktop-file-validate data/$(APP_ID).desktop
	appstreamcli validate --no-net --explain data/$(APP_ID).metainfo.xml

.PHONY: check
check: lint validate ## Run all local checks

# --- Flatpak -----------------------------------------------------------------

.PHONY: flatpak-deps
flatpak-deps: ## Install flatpak-builder and the GNOME SDK (user installation)
	flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
	flatpak install --user -y flathub org.flatpak.Builder org.gnome.Sdk//$(SDK) org.gnome.Platform//$(SDK)

.PHONY: flatpak
flatpak: ## Build the Flatpak and install it for the current user
	$(BUILDER) --user --install --force-clean --install-deps-from=flathub \
		--repo=repo build-dir $(MANIFEST)

.PHONY: flatpak-build
flatpak-build: ## Build the Flatpak into ./repo without installing it
	$(BUILDER) --user --force-clean --install-deps-from=flathub \
		--repo=repo build-dir $(MANIFEST)

.PHONY: flatpak-run
flatpak-run: ## Run the installed Flatpak
	flatpak run $(APP_ID)

.PHONY: flatpak-bundle
flatpak-bundle: flatpak-build ## Create a single-file .flatpak bundle to share or install elsewhere
	flatpak build-bundle repo $(BUNDLE) $(APP_ID) \
		--runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo
	@echo "Install with: flatpak install --user $(BUNDLE)"

.PHONY: flatpak-lint
flatpak-lint: ## Run Flathub's linter (host filesystem access needs a Flathub exception)
	$(LINT) manifest $(MANIFEST) || true
	$(LINT) repo repo || true

.PHONY: flatpak-uninstall
flatpak-uninstall: ## Remove the locally installed Flatpak
	flatpak uninstall --user -y $(APP_ID) $(APP_ID).Debug || true

# --- Housekeeping ------------------------------------------------------------

.PHONY: clean
clean: ## Remove Flatpak build output (keeps the build cache)
	rm -rf build-dir repo $(BUNDLE)

.PHONY: distclean
distclean: clean ## Also remove the Flatpak build cache and the venv
	rm -rf .flatpak-builder .venv .ruff_cache
