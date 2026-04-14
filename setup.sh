#!/bin/bash
# One-time setup: creates venv, installs dependencies
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Setting up Crossover Appointment Checker..."

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python packages
pip install --quiet --upgrade pip
pip install playwright python-dotenv

# Install Chromium browser
playwright install chromium

# Create .env if it doesn't exist
if [ ! -f .env ]; then
    cp .env.template .env
    echo ""
    echo "⚠️  Created .env — fill in your credentials:"
    echo "    open $SCRIPT_DIR/.env"
else
    echo ".env already exists — skipping"
fi

echo ""
echo "Setup complete. Next steps:"
echo "  1. Edit .env with your Crossover credentials"
echo "  2. Run a test login:  ./debug.sh"
echo "  3. Enable scheduling: ./enable.sh"
