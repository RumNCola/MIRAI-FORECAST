
if not exist ".venv" (
    python -m venv .venv
)

call .venv/scripts/activate

python.exe -m pip install --upgrade pip

pip install -r requirements.txt

python main.py