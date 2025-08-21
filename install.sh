#!/bin/bash

set -e

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_info "Installing GPU Occupy tool..."

# Install uv if not present
if ! command -v uv &> /dev/null; then
    print_info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Create virtual environment
print_info "Creating virtual environment..."
uv venv gpu-occupy-env

# Install PyTorch
print_info "Installing PyTorch..."
uv pip install --python gpu-occupy-env/bin/python PyYAML
uv pip install --python gpu-occupy-env/bin/python numpy
uv pip install --python gpu-occupy-env/bin/python torch

# Set permissions
chmod +x gpu_occupy.py occupy

# Create wrapper script
print_info "Creating wrapper script..."
CURRENT_DIR="$(pwd)"
cat > occupy_env << EOF
#!/bin/bash
source "$CURRENT_DIR/gpu-occupy-env/bin/activate"
exec "$CURRENT_DIR/occupy" "\$@"
EOF
chmod +x occupy_env

# Create symlink to ~/.local/bin if exists
if [ -d "$HOME/.local/bin" ]; then
    ln -sf "$(pwd)/occupy_env" "$HOME/.local/bin/occupy"
    print_success "Symlink created: ~/.local/bin/occupy"
fi

# Test installation
if ./occupy_env --help > /dev/null 2>&1; then
    print_success "Installation complete!"
    echo
    echo "Usage:"
    echo "  occupy on     # Start GPU occupation"
    echo "  occupy off    # Stop GPU occupation"
    echo "  occupy status # Check status"
else
    print_error "Installation test failed"
    exit 1
fi