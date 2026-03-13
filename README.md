# Dedicated Immich Plugin for InkyPi

Display photos from an Immich album on InkyPi, with optional tag filtering, captions, padding, image adjustments, and LUT-based color presets.

## 🚀 Features

- Connects to an Immich server with a Base URL and API key
- Loads album names dynamically
- Loads tag suggestions dynamically
- Optional tag filtering
- Optional caption overlay from:
  - IPTC image metadata
  - Immich asset description
- Image enhancement controls:
  - saturation
  - brightness
  - contrast
  - sharpness
- Color presets support via `lut.json`

## Screenshot

![screenshot](https://github.com/doowylloh88/Immich/blob/main/immich/docs/images/immich_menu.jpg)

![screenshot](https://github.com/doowylloh88/Immich/blob/main/immich/docs/images/zuma.png)

## 🛠️ Installation

1.  Install [Immich](https://pimylifeup.com/raspberry-pi-immich/)
2. Set your Immich API key in the environment / .env file :
```env
IMMICH_KEY=your_api_key_here
```
3. Install the plugin using the InkyPi CLI, providing the plugin ID & GitHub repository URL:

```bash
inkypi plugin install immich https://github.com/doowylloh88/immich
```

## How it works

1.  Enter your Immich Base URL
    
2.  The plugin validates the URL and loads available albums & tags
    
3.  Select an album
    
4.  Optionally enter a tag filter
    
5.  Optionally enable captions
    
6.  Optionally choose a LUT / color preset from the drop-down
    
7.  The plugin fetches an image, processes it, and returns it for display

## Caveats

- To be recognized, the caption text must contain square brackets
	- ```[Malibu, CA] ```
- Captions / Keywords and tags can be entered either in Immich or using your favorite photo editor (IPTC)
- The LUTs or more like color presets can be edited in the lut.json file
- Some of the presets are based on [Inky Photo Frame ](https://github.com/mehdi7129) I highly suggest you tweak them based on your Spectra6's screen
- The sliders for saturation, brightness, etc. will carry over to the main settings screen, but they will not be saved. I haven’t found a way around that yet. They also do not seem to affect other modules
- Speaking of sliding, this plug-in was 100% created using vibe- coding & a lot of yelling at ChatGPT.  An actual coder should take over the project to maintain it
