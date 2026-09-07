"""Export this project's Windows user variables to the ignored local .env."""

from pathlib import Path
import winreg

from runtime_config import PROJECT_VARIABLES


def main():
    values = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        for name in sorted(PROJECT_VARIABLES):
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                continue
            if value:
                values[name] = str(value)

    target = Path(__file__).resolve().parent / ".env"
    lines = [
        "# Generated from Windows user environment by sync_windows_env.py.",
        "# This file is ignored by Git. Do not commit or share it.",
        *[f"{name}={value}" for name, value in values.items()],
        "",
    ]
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"Saved {len(values)} variables to {target}")


if __name__ == "__main__":
    main()
