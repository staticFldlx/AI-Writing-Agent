# DiceBot 一键打包脚本
# 用法：在本目录运行  powershell -ExecutionPolicy Bypass -File build.ps1
# 产物：dist\DiceBot.exe —— 单文件，目标机器无需安装 Python，双击即用
#
# 打包的是统一入口 app.py（写作助手 + 骰子四个面板）。
# sqlite3 / urllib 都是标准库，无需额外 hidden-import；
# 用户数据（配置、密钥、设定库、对话）都在 %APPDATA%\DiceBot\，不进 exe。

pyinstaller --noconfirm --clean --onefile --windowed --name DiceBot `
    --collect-all customtkinter `
    app.py

# 若杀毒软件对单文件 exe 误报，改用目录模式并打成 zip 分发：
#   pyinstaller --noconfirm --clean --onedir --windowed --name DiceBot --collect-all customtkinter app.py
#   Compress-Archive -Path dist\DiceBot -DestinationPath dist\DiceBot.zip -Force
