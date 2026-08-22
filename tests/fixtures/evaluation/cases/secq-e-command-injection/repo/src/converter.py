import subprocess


def convert_upload(user_filename: str) -> None:
    # user_filename is taken directly from an HTTP upload form field.
    subprocess.run(f"convert {user_filename} {user_filename}.png", shell=True, check=False)


def convert_fixed_asset() -> None:
    subprocess.run(["convert", "assets/logo.svg", "assets/logo.png"], check=False)
