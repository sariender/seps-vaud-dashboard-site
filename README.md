# SEPS Vaud dashboard

Portrait dashboard for Lausanne and Ouchy, designed for a 57 × 96.5 cm screen.

- **Current mode: frozen at 16:11 on 20 November 2026 (Europe/Zurich)** for design review. The clock and data use one [saved capture](data/dashboard-frozen.json).
- Automatic updates and category rotation are off. Category buttons still work. The live update rules below apply when live mode is resumed.

## Header

- Europe/Zurich date and time; clock updates every second.

## Weather row

- Temperature, humidity and wind: [MeteoSwiss Pully observations](https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/puy/ogd-smn_puy_t_now.csv), refreshed hourly. Use a complete observation no more than 30 minutes old at fetch time. [Station documentation](https://opendatadocs.meteoswiss.ch/a-data-groundbased/a1-automatic-weather-stations).
- Weather icon: Open-Meteo's `weather_code` and `is_day` for the nearest Ouchy forecast grid cell, using MeteoSwiss ICON CH1. Refresh hourly with day/night symbols. [Variable definitions](https://open-meteo.com/en/docs/meteoswiss-api).

## WBGT and advice

- Calculate outdoor WBGT from Pully temperature, humidity, wind, solar radiation and pressure using [Liljegren et al. (2008)](https://doi.org/10.1080/15459620802310770), implemented in [`lwbgt` 1.2.1](https://github.com/zyf0717/lwbgt).
- Settings: 10-minute averaging and 10 m wind; fixed assumptions `urban=1` and `vertical_temperature_difference_c=1`. [Input assumptions](https://github.com/zyf0717/lwbgt#input-assumptions).
- Recalculate hourly. The **−6°C to 33°C** gauge range is a display choice; classify the calculated WBGT before rounding.
- Thresholds follow the **Japan Sports Association's 2019 exercise guidance**, reproduced by [Japan's Ministry of the Environment](https://www.wbgt.env.go.jp/en/wbgt.php):
  - **Low:** WBGT < 21°C.
  - **Caution:** 21°C ≤ WBGT < 25°C.
  - **Warning:** 25°C ≤ WBGT < 28°C.
  - **Severe:** 28°C ≤ WBGT < 31°C.
  - **Danger:** WBGT ≥ 31°C.
- Assign the higher category at a boundary: exactly 25°C is Warning. “Low” replaces the source's “Almost safe”; “Severe” shortens “Severe Warning”. Advice messages are dashboard adaptations of this exercise guidance. [Assignment code](server.py).

## Map temperatures

- Colours use **MeteoSwiss ICON CH1 numerical weather forecasts**, supplied through [Open-Meteo's MeteoSwiss API](https://open-meteo.com/en/docs/meteoswiss-api): `models=meteoswiss_icon_ch1`, variable `temperature_2m` (air temperature 2 m above ground, °C), for the current UTC forecast hour.
- Open-Meteo supplies a **0.01° (about 1 km) forecast grid**. Fetch hourly; new ICON CH1 model runs are issued every **3 hours**. [Resolution and update frequency](https://open-meteo.com/en/docs/meteoswiss-api).
- Select the nearest forecast grid cells (`cell_selection=nearest`), with elevation downscaling disabled (`elevation=nan`). [API parameters](https://open-meteo.com/en/docs).
- Linearly interpolate these ICON CH1 temperatures across the illustration with a **50% land tint**. Colours represent interpolated model forecasts at about 1 km resolution. Thermometer colours and Celsius labels use the same local temperature scale.

## Nearby places

- Fixed illustration; rotate water points, outdoor spaces, outdoor spaces and playgrounds, swimming, and indoor spaces every **15 seconds**.
- Start with water on reload. Manual selection restarts the timer. Non-water locations require manual source-list updates.

## Drinking water

- Check the [Lausanne register](https://map.lausanne.ch/) hourly. Show **potable, open** points within calibrated map coverage; exclude showers.
- Illustrated positions are approximate; links open City map locations. Register status does not confirm water flow.

## Live update rules

- GitHub Pages: an hourly GitHub Actions job fetches data and publishes JSON snapshots. The browser reads these every minute; updates create no Git commits.
- Local Python server: fetch at startup, then hourly. Browser checks cached data every minute.
- Expire live data after **61 minutes 20 seconds** without a successful refresh. Hide affected values, advice and layers; keep the base map.
- Failed updates retain original timestamps. Failed startup configuration retries each minute; saved preview values never substitute for live data.
- Bottom block stays empty.
- GitHub schedules can be delayed and may pause after 60 days without repository activity. [Schedule rules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). [Publishing setup](deploy/github-pages/README.md).
