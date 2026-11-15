# Plant display

A plant-watering picture for three e-ink screens, built in Home Assistant on November 12, 2026.
Nothing in this repository renders or deploys it: the dashboard, the helpers and the automation live in Home Assistant, and Home Assistant holds the only copy of their configuration.
This page records what exists, how to change it, and what to expect when it goes wrong.

## What it shows

Four plant pots in one landscape row.
Each pot fills with solid black in proportion to the plant's soil moisture, and carries a dashed line at the plant's watering limit.
Under each pot are the plant's name, its moisture percentage and a battery meter for its sensor.
The corner shows the weekday and time of the render, so a screen that stopped updating does not pass as current.

| State | What the pot shows |
|---|---|
| Healthy | The fill above the dashed line, and nothing above the pot. |
| Needs water | A large water drop above the pot and a black `WATER` banner below it. |
| Dead or silent sensor | A crossed-out pot, `NO SIGNAL`, and `--` for the reading. |

The picture is black and white only, with no greys and no dithering.

## What exists in Home Assistant

| Kind | Name | Purpose |
|---|---|---|
| Dashboard | `Plant display`, URL `plant-display`, view `eink` | One Markdown card whose template draws the whole picture as a single SVG image. |
| Threshold helpers | One per plant, named `<Plant> thirsty` | Decide whether the plant needs water. |
| Automation | `Plant display e-ink tag` | Redraws the OpenEPaperLink tag. |
| TRMNL HA app schedules | `Plant display - TRMNL` and `Plant display - Kindle` | Push the picture to the TRMNL and the Kindle. You create these by hand; see below. |

No template sensor, script or frontend resource was added.
The card uses card-mod for three lines of sizing CSS and kiosk-mode to hide the header; both were already installed.

## How it works

The TRMNL HA app opens the dashboard view in a headless browser at each screen's own pixel size and captures it.
The picture is a 4:3 drawing sized from the screen height, so it fills the 4:3 screens and sits between white margins on the wider TRMNL.

| Screen | Size | How the image arrives |
|---|---|---|
| OpenEPaperLink tag, 4.2 inch | 400x300 | The automation calls `open_epaper_link.drawcustom` with one `dlimg` element that downloads the app's render. |
| TRMNL | 800x480 | An app schedule pushes it. |
| Kindle Voyage, landscape | 1448x1072 | An app schedule pushes it. |

The automation redraws the tag at 00:00, 06:00, 12:00 and 18:00, and whenever a Threshold helper changes between on and off.
It downloads this address, which resolves only inside Home Assistant:

```
http://0b3326fa-trmnl-ha:10000/plant-display/eink?viewport=400x300&dithering&dither_method=threshold&palette=bw&no_normalize&format=png&wait=1500&kiosk
```

Three of those parameters are not obvious.
Without `dithering` the app ignores `palette` and returns colour.
Without `no_normalize` a blank or failed render is stretched to an all-black screen.
`kiosk` is not an app parameter: the app forwards it to the page, where kiosk-mode hides the header.

### When a plant needs water

Each Threshold helper watches its plant's moisture sensor with a lower limit of 30 and a hysteresis of 2.
It turns on below 28, turns off above 32, and keeps its state in between, which absorbs the one or two points of hourly sensor noise.
The banner and the drop come from the helper.
The fill and the percentage show the raw reading.
The dashed line is drawn at the helper's own lower limit.

### When a sensor counts as dead

A plant shows `NO SIGNAL` when any of these is true:

1. Its moisture or battery reading is not a number.
2. Its battery reads 0 or less.
3. No sensor on the plant's device has updated for 24 hours.

`NO SIGNAL` takes precedence over `WATER`.
Rule 3 reads every sensor on the device, because an unchanged moisture value does not update its own timestamp.

## Change it

To change one plant's watering limit, edit the lower limit of its Threshold helper under **Settings** > **Devices & services** > **Helpers**.
The pot's dashed line and its banner both follow, with no other edit.

To rename a plant on the display, edit its label in the `plants` list at the top of the card's template, in the dashboard's raw configuration.

To add a plant:

1. Create a Threshold helper for its moisture sensor, with a lower limit and a hysteresis of 2.
2. Add a row to the `plants` list in the card's template.
3. Add the helper to the automation's state trigger.

The drawing has room for four columns, so a fifth plant also needs the column width changed in the template.

## Create the two app schedules

Open the TRMNL HA app from the Home Assistant sidebar.
Leave every field not listed at its default.
Never write a webhook address into this repository.

For the TRMNL:

1. Set **Name** to `Plant display - TRMNL`.
2. Set **Dashboard Path** to `/plant-display/eink?kiosk`.
3. Set **Width** to `800` and **Height** to `480`.
4. Set **Schedule** to cron `0 * * * *`.
5. Set **Webhook URL** to the TRMNL's existing webhook.
6. Set **Format** to `PNG` and **Rotation** to `None`.
7. Set **Wait (ms)** to `1500`.
8. Tick **Enable Dithering**.
9. Set **Method** to `None` and **Palette** to `1-bit (B&W)`.
10. Untick **Normalize**.
11. Save.
12. Press **Send Now** and look at the screen.

For the Kindle:

1. Set **Device preset** to `Amazon Kindle Voyage`. The preset sets the 8-bit output the Kindle needs, and the form has no field for it; without it the Kindle can show a blank screen.
2. Set **Name** to `Plant display - Kindle`.
3. Set **Dashboard Path** to `/plant-display/eink?kiosk`.
4. Check that **Width** is `1448` and **Height** is `1072`.
5. Set **Schedule** to cron `0 * * * *`.
6. Set **Webhook URL** and **Webhook Format** to the Kindle's existing endpoint.
7. Set **Wait (ms)** to `1500`.
8. Tick **Enable Dithering**.
9. Set **Method** to `None` and **Palette** to `1-bit (B&W)`.
10. Untick **Normalize**.
11. Save.
12. Press **Send Now** and look at the screen.
13. If the picture is upside down, change **Rotation** from `90°` to `270°`.

## Sharp edges

- **A moisture reading pinned at 100 is the sensor, not the display.** The sensors clamp at their calibrated wet point, so soil wetter than that point reads 100 until it dries past it. Two of the four read 100 for a week in November 2026 while their devices reported hourly.
- **A dead sensor can hide for 24 hours after a restart.** A Home Assistant restart or an MQTT reload refreshes every sensor's timestamp, so rule 3 does not fire until 24 hours later. A sensor whose battery reads 0 is still caught at once.
- **A helper forgets `WATER` across a restart.** A plant reading 28 to 32 that needed water before a restart comes back as healthy, until it falls below 28 again.
- **A single false reading draws `WATER` once.** One sensor reported a lone 0 between two normal readings. The banner clears with the next reading, about an hour later.
- **A hung app stops the tag silently.** The image download has no timeout, so the automation waits and later redraws are dropped. The tag keeps its old picture with its old weekday and time. Restart the TRMNL HA app to clear it.
- **The tag's image entity in Home Assistant can lag the tag.** After a draw it held the previous picture for more than six minutes. Trust the tag, not the entity.
- **All three screens go blank together if the frontend stops rendering the card's image.** The picture reaches the card as a `data:image/svg+xml` image, which the Markdown card's filter admits. If a frontend update changes that, the fallback is the button-card custom card with the same SVG inline. If only the render arrives late, raise `wait` to `3000`.
- **Without card-mod the picture shrinks.** The TRMNL and the Kindle show a small picture in the top-left corner; the tag is unaffected.
- **The TRMNL HA app's token must stay a `!secret` reference.** The app signs in to Home Assistant with a long-lived access token. Its `access_token` option holds `!secret trmnl_ha_token`, and the token belongs to a dedicated non-administrator user. A lookup of the app's details through the Home Assistant MCP server (`ha_get_app`) returns the stored option, so a token pasted there in clear is printed into the agent's transcript; that happened in November 2026.

## Remove it

Remove in this order, because the automation and the dashboard both name the helpers.

1. Delete the automation `Plant display e-ink tag`.
2. Delete the dashboard `Plant display`.
3. Delete the Threshold helpers named `<Plant> thirsty`.
4. Delete the two `Plant display` schedules in the TRMNL HA app.

The tag keeps its last picture until something else draws on it.
