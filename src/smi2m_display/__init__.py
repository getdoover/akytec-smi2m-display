from pydoover.docker import run_app

from .application import SMI2MApplication


def main():
    """Run the application."""
    run_app(SMI2MApplication())
