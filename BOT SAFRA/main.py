from src.bot import create_bot


def main() -> None:
    bot = create_bot()
    bot.run_from_env()


if __name__ == "__main__":
    main()
