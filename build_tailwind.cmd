@echo off
cd /d "%~dp0"
echo Building Tailwind CSS from templates + static/js ...
npx --yes tailwindcss@3.4.17 -i static/css/tailwind.src.css -o static/css/tailwind.css --minify
if errorlevel 1 pause
