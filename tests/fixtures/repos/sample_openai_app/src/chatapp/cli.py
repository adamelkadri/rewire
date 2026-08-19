"""Command line front end."""

from chatapp.client import ChatClient


def main() -> None:
    print(ChatClient().generate("hello"))


if __name__ == "__main__":
    main()
