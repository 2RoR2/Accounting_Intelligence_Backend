"""Compatibility entrypoint for the administrative command."""

from services.admin import manage as _manage

connect = _manage.connect


def main():
    # Tests and local tooling historically patch ``manage.connect``. Mirror
    # that binding into the implementation before dispatching the command.
    _manage.connect = connect
    return _manage.main()


if __name__ == "__main__":
    main()
