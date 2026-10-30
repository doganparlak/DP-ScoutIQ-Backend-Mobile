"""Fit and activate the shared current-season model: python -m scripts.train_match_forecast."""
import json

from api_module.database import engine  # Loads the backend environment.
from match_pool_module.forecast import train_shared_model


def main():
    result = train_shared_model()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
