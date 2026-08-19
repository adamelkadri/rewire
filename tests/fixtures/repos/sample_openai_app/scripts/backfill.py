"""A standalone script with its own main guard."""

from chatapp.client import summarise

if __name__ == "__main__":
    print(summarise("text"))
