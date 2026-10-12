# eFlesh Visualizer

```pip install reskin-sensor anyskin pygame matplotlib numpy==1.26.4```

1. Connect the magnetometer circuit board to the microcontroller using the provided QWIIC cable.

2. Connect the microcontroller (Adafruit QT Py) to your computer using a USB-C cable.

3. Find the dev path/COM port your microcontroller is connected to:
<br><br> <b>Linux:</b> `ls /dev/ | grep -e ACM -e USB`. This is generally `/dev/ttyACM0` or `/dev/ttyUSB0`. <br>If you get a `can't open device "<port>": Permission denied` error, modify permissions to allow read and write on that port: `sudo chmod a+rw <port>`
<br> <b>MacOS:</b> `ls /dev/ | grep cu.usb`. This is generally `cu.usbmodem*`.
<br> <b>Windows:</b> Open Device Manager. Click `View` and select `Show Hidden Devices`. Locate `Ports (COM & LPT)`. Note the `COM` port corresponding to the QT Py.
<br><br>If you have no other devices connected, this should give you a single path. If you see multiple, disconnect the microcontroller and run the command again. Reconnect the microcontroller and re-run the command. The additional path is your `<port>`.

## Tuner

`eflesh_tuner.py` changes the magnetometer settings live (gain, resolution,
digital filter, oversampling, temperature compensation, frame period) and plots
the effect, with per-chip mean, noise in µT and LSB, and read-failure counts.

```pip install PySide6 pyqtgraph pyserial numpy```

Flash [`../arduino/eflesh_cmd_stream`](../arduino/eflesh_cmd_stream) first: the
older sketches do not accept commands. Then:

```
python visualizer/eflesh_tuner.py -p /dev/ttyACM0
python visualizer/eflesh_tuner.py --sim       # simulated firmware, no hardware
```

The settings panel is built from the firmware's own settings table, so a
setting added to the sketch shows up here without a Python change. Settings
live in the board's RAM and reset to the defaults on power-up.
