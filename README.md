# 🍄 Mushroom Pi 🍓 - Pico Unit

![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)![Raspberry Pi](https://img.shields.io/badge/-Raspberry_Pi-C51A4A?style=for-the-badge&logo=Raspberry-Pi)

A **MicroPython** repo for **Raspberry Pi Pico 2 W** to:

1. Control humidity and temperature conditions in a mushroom growing unit.
2. Serve the data via a REST API.

## Required libraries

This repo requires using some extra libraries.

### Using MicroPython's package management

As explained [in Micropython's documentation](https://docs.micropython.org/en/latest/reference/packages.html) packages can be added with `mip`:

```python
  >>> import mip
  >>> mip.install(urequests)
  >>> mip.install(microdot)
```

### Adding manually

Extra packages can be added as single files in the `lib/` folder at the root of the package on execution.

- `urequests.py`: download from [Github](https://github.com/lucien2k/wipy-urllib/blob/master/urequests.py).
- `microdot.py`: download from [Github](https://github.com/miguelgrinberg/microdot/blob/main/src/microdot/microdot.py).

## How to use

### LED indicator

This software allows minimal communication through the Pico's intergrated LED. When the software starts executing, the LED will ALWAYS turn on. After that first flash, the possible LED statuses are:

- LED OFF: The software has been executed, but the provided Wifi credentials haven't allowed connecting the device to the internet. Review the Wifi settings if you want to access the unit's records.
- LED ON: The device has successfully connected to the provided Wifi, but hasn't been able to communicate with the local hub.
- LED BLINKING: The unit is connected to the Wifi and can see and communicate with the local hub.
