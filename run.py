"""Run the approved dashboard with hourly weather, water and temperature checks."""

import sys

import server


if __name__ == "__main__":
    server.main(["--live", "--live-water", *sys.argv[1:]])
