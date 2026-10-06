"""Native Windows file/folder pickers, run out-of-process so nothing can hang."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_FILE_PS = r"""
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = 'Choose a stream recording'
$d.Filter = 'Video files|*.mp4;*.mkv;*.mov;*.webm;*.flv;*.ts;*.avi;*.m4v;*.mpg;*.wmv|All files|*.*'
$d.Multiselect = $false
$top = New-Object System.Windows.Forms.Form
$top.TopMost = $true
if ($d.ShowDialog($top) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.FileName }
$top.Dispose()
"""

_FOLDER_PS = r"""
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Choose where finished clips are saved'
$d.ShowNewFolderButton = $true
$top = New-Object System.Windows.Forms.Form
$top.TopMost = $true
if ($d.ShowDialog($top) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }
$top.Dispose()
"""


def _run_ps(script: str, timeout: int = 300) -> str:
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
        )
        return (out.stdout or "").strip().splitlines()[-1].strip() if out.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def pick_file() -> str:
    path = _run_ps(_FILE_PS)
    return path if path and Path(path).is_file() else ""


def pick_folder() -> str:
    path = _run_ps(_FOLDER_PS)
    return path if path and Path(path).is_dir() else ""


def reveal(path: str) -> bool:
    """Open a folder in Explorer, selecting the file if one was given."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        if sys.platform == "win32":
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                subprocess.Popen(["explorer", str(p)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p if p.is_dir() else p.parent)])
        else:
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
        return True
    except OSError:
        return False
