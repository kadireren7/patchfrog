raise SystemExit("setup.py must never be executed by discovery")
from setuptools import setup

setup(name="x", install_requires=["fastapi==0.1.0"])
