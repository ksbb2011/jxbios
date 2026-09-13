# Copy business layer from robot_order_picker_new (baseline) to robot_order_picker_imouse
# Run: powershell -ExecutionPolicy Bypass -File tools/copy_business_layer.ps1
# Verify: py -3.11 -c "import core.flow.task; print('IMPORT_OK')"
# NOTE: keep this file pure ASCII - PowerShell parses ps1 as GBK and Chinese strings break parsing.

$src = "F:\JXBproject\robot_order_picker_new"
$dst = "F:\JXBproject\robot_order_picker_imouse"

Copy-Item -Recurse -Force "$src\core\domain"                 "$dst\core\domain"
Copy-Item -Recurse -Force "$src\core\flow"                   "$dst\core\flow"
Copy-Item -Force      "$src\core\vision\ocr.py"              "$dst\core\vision\ocr.py"
Copy-Item -Force      "$src\core\vision\stable_state.py"     "$dst\core\vision\stable_state.py"
Copy-Item -Force      "$src\main.py"                         "$dst\main.py"

Get-ChildItem "$dst\core\domain", "$dst\core\flow" -Recurse -Directory -Filter __pycache__ |
    Remove-Item -Recurse -Force

Write-Host "Business layer copied. Next: py -3.11 -c 'import core.flow.task'"
