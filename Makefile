VENV_DIR := .venv
ACTIVATE_SCRIPT := $(VENV_DIR)/bin/activate

.PHONY: help venv install activate clean shell

# Help command to display usage
help:
	@echo ""
	@echo "Makefile commands:"
	@echo ""
	@echo "  make venv      - Create a virtual environment in $(VENV_DIR)"
	@echo "  make install   - Create virtual environment and install dependencies"
	@echo "  make activate  - Instructions to manually activate the virtual environment"
	@echo "  make clean     - Remove the virtual environment"
	@echo "  make shell     - Spawn a new shell with virtual environment activated"
	@echo ""

venv:
	@echo "🔧 Creating virtual environment in $(VENV_DIR)..."
	uv venv $(VENV_DIR)

install: venv
	@echo "📦 Installing dependencies..."
	uv pip install -r requirements.txt
	@echo
	@echo "✅ Dependencies installed."
	@echo "👉 Spawning a new shell inside the virtual environment..."
	@$(MAKE) shell

shell:
	@echo "Spawning a new shell with virtualenv activated..."
	@bash -c "source $(ACTIVATE_SCRIPT); exec bash"

activate:
	@echo "👉 To activate the virtual environment manually, run:"
	@echo "source $(ACTIVATE_SCRIPT)"

clean:
	@echo "🧹 Removing virtual environment..."
	rm -rf $(VENV_DIR)
	@echo "✅ Done."
