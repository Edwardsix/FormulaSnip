$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller==6.22.3
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name FormulaSnip --collect-data latex2mathml --exclude-module numpy --exclude-module matplotlib --exclude-module scipy --exclude-module pandas app.py
Copy-Item -LiteralPath '使用说明.html','README.txt','第三方许可.txt','界面预览.png','设置预览.png' -Destination dist -ErrorAction SilentlyContinue
Write-Host '完成：dist\FormulaSnip.exe'
