#!/usr/bin/env python

import time
import numpy as np
import os
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import glob
import pygame
import platform
import matplotlib.pyplot as plt
from reskin_sensor import ReSkinProcess

def get_val_port(provided_port):
    if provided_port:
        return provided_port if os.path.exists(provided_port) else (print(f"Error: The specified port '{provided_port}' does not exist.") and exit(1))

    os_name = platform.system()
    candidates = []

    candidates = glob.glob("/dev/ttyACM*") if os_name == "Linux" else glob.glob("/dev/cu.usbmodem*") if os_name == "Darwin" else (print("Specify the port manually.") and exit(1))

    if len(candidates) > 1:
        prioritized_ports = ["/dev/ttyACM0", "/dev/cu.usbmodem101"]
        for port in prioritized_ports:
            if port in candidates:
                return port
        return candidates[0]
    elif len(candidates) == 1:
        return candidates[0]
    else:
        print("Error: No valid ports detected. Please specify the port manually.")
        exit(1)

def plot_visualizer(port, baudrate=9600, runtime=180, num_mags=5, record=True):
    """
    Real-time rolling plot visualizer
    """
    sensor_stream = ReSkinProcess(
        num_mags=num_mags,
        port=port,
        baudrate=baudrate,
        temp_filtered=True,
    )
    sensor_stream.start()
    time.sleep(0.1)

    def get_baseline():
        baseline_data = sensor_stream.get_data(num_samples=5)
        return np.mean([reading.data for reading in baseline_data], axis=0)

    time.sleep(0.4)
    baseline = get_baseline()

    plt.ion()
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.set_title("Real-Time ReSkin Data Visualization")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Normalized Sensor Values")

    time_data = []
    bx0, by0, bz0 = [], [], []
    bx1, by1, bz1 = [], [], []
    bx2, by2, bz2 = [], [], []
    bx3, by3, bz3 = [], [], []
    bx4, by4, bz4 = [], [], []

    y_min, y_max = -400, 400

    if record:
        filename = f"logs/{time.strftime('%Y-%m-%d')}/run_{time.strftime('%H-%M-%S')}.txt"
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        recorded_data = []

    bx0_line, = ax.plot([], [], label="Bx0 (Solid)", linestyle="-", color="r")
    by0_line, = ax.plot([], [], label="By0 (Solid)", linestyle="-", color="g")
    bz0_line, = ax.plot([], [], label="Bz0 (Solid)", linestyle="-", color="b")
    bx1_line, = ax.plot([], [], label="Bx1 (Dashed)", linestyle="--", color="r")
    by1_line, = ax.plot([], [], label="By1 (Dashed)", linestyle="--", color="g")
    bz1_line, = ax.plot([], [], label="Bz1 (Dashed)", linestyle="--", color="b")
    bx2_line, = ax.plot([], [], label="Bx2 (Dotted)", linestyle=":", color="r")
    by2_line, = ax.plot([], [], label="By2 (Dotted)", linestyle=":", color="g")
    bz2_line, = ax.plot([], [], label="Bz2 (Dotted)", linestyle=":", color="b")
    bx3_line, = ax.plot([], [], label="Bx3 (DashDot)", linestyle="-.", color="r")
    by3_line, = ax.plot([], [], label="By3 (DashDot)", linestyle="-.", color="g")
    bz3_line, = ax.plot([], [], label="Bz3 (DashDot)", linestyle="-.", color="b")
    bx4_line, = ax.plot([], [], label="Bx4 (Solid)", linestyle="-", color="c")
    by4_line, = ax.plot([], [], label="By4 (Solid)", linestyle="-", color="m")
    bz4_line, = ax.plot([], [], label="Bz4 (Solid)", linestyle="-", color="y")

    ax.legend()
    start_time = time.time()

    try:
        while time.time() - start_time < runtime:
            sensor_data = sensor_stream.get_data(num_samples=1)
            if not sensor_data:
                continue

            current_time = time.time() - start_time
            time_data.append(current_time)

            fig.canvas.flush_events()
            if plt.waitforbuttonpress(timeout=0.001):
                if plt.get_current_fig_manager().toolbar.mode == '':
                    print("Subtracting baseline")
                    baseline = get_baseline()

            if sensor_data:
                data = np.array(sensor_data[0].data) - baseline
            bx0.append(data[0])
            by0.append(data[1])
            bz0.append(data[2])
            bx1.append(data[3])
            by1.append(data[4])
            bz1.append(data[5])
            bx2.append(data[6])
            by2.append(data[7])
            bz2.append(data[8])
            bx3.append(data[9])
            by3.append(data[10])
            bz3.append(data[11])
            bx4.append(data[12])
            by4.append(data[13])
            bz4.append(data[14])

            if record:
                recorded_data.append([current_time] + data.tolist())

            window_size = 10
            while time_data and (time_data[-1] - time_data[0]) > window_size:
                time_data.pop(0)
                bx0.pop(0)
                by0.pop(0)
                bz0.pop(0)
                bx1.pop(0)
                by1.pop(0)
                bz1.pop(0)
                bx2.pop(0)
                by2.pop(0)
                bz2.pop(0)
                bx3.pop(0)
                by3.pop(0)
                bz3.pop(0)
                bx4.pop(0)
                by4.pop(0)
                bz4.pop(0)

            all_data = np.concatenate([bx0, by0, bz0, bx1, by1, bz1, bx2, by2, bz2, bx3, by3, bz3, bx4, by4, bz4])
            min_val, max_val = np.min(all_data), np.max(all_data)
            y_min = min(y_min, min_val - 0.1)
            y_max = max(y_max, max_val + 0.1)
            ax.set_ylim(y_min, y_max)

            bx0_line.set_data(time_data, bx0)
            by0_line.set_data(time_data, by0)
            bz0_line.set_data(time_data, bz0)
            bx1_line.set_data(time_data, bx1)
            by1_line.set_data(time_data, by1)
            bz1_line.set_data(time_data, bz1)
            bx2_line.set_data(time_data, bx2)
            by2_line.set_data(time_data, by2)
            bz2_line.set_data(time_data, bz2)
            bx3_line.set_data(time_data, bx3)
            by3_line.set_data(time_data, by3)
            bz3_line.set_data(time_data, bz3)
            bx4_line.set_data(time_data, bx4)
            by4_line.set_data(time_data, by4)
            bz4_line.set_data(time_data, bz4)

            if time_data:
                ax.set_xlim(time_data[0], time_data[-1])

            fig.canvas.draw()

    except KeyboardInterrupt:
        print("\nSaving data...")

    finally:
        sensor_stream.pause_streaming()
        sensor_stream.join()

        if record and 'recorded_data' in locals() and recorded_data:
            # One column per channel actually written: time + num_mags * (x, y, z)
            header = ",".join(
                ["Time"] + [f"B{axis}{mag}" for mag in range(num_mags) for axis in "xyz"]
            )
            np.savetxt(filename, np.array(recorded_data), header=header, delimiter=",")
            print(f"Recorded data saved to {filename}")
        else:
            print("No data recorded or saved.")

def graphic_visualizer(port, baudrate, num_mags=1, scaling=7.0, runtime=180, record=True):
    """
    Glove visualizer for a single magnetometer (thumb only).
    """
    pygame.init()
    window_width, window_height = 720, 1080
    window = pygame.display.set_mode((window_width, window_height), pygame.SRCALPHA)
    filename = f"logs/{time.strftime('%Y-%m-%d')}/run_{time.strftime('%H-%M-%S')}.txt"
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    pygame.display.set_caption("Graphic Visualization")

    dir_path = os.path.dirname(os.path.realpath(__file__))
    glove_image_path = os.path.join(dir_path, "ruka_eflesh.png")
    glove_image = pygame.image.load(glove_image_path)
    glove_image = pygame.transform.scale(glove_image, (window_width, window_height))

    # Update chip locations and rotations for a single sensor (thumb)
    chip_locations = np.array([
        [437, 93],  # Thumb location
    ])
    chip_xy_rotations = np.array([3 * np.pi / 2])  # Thumb rotation

    sensor_stream = ReSkinProcess(
        num_mags=num_mags,
        port=port,
        baudrate=baudrate,
        temp_filtered=True,
    )
    sensor_stream.start()

    def get_baseline():
        baseline_data = sensor_stream.get_data(num_samples=5)
        return np.mean([reading.data for reading in baseline_data], axis=0)

    time.sleep(0.4)
    baseline = get_baseline()
    running = True
    all_data = []
    clock = pygame.time.Clock()
    start_time = time.time()

    try:
        while running:
            window.fill((255, 255, 255))
            flipped_glove_image = pygame.transform.flip(glove_image, True, False)
            window.blit(glove_image, (0, 0))

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                if event.type == pygame.MOUSEBUTTONDOWN:
                    x, y = pygame.mouse.get_pos()
                    print(f"Mouse clicked at ({x}, {y})")
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_b:
                        baseline = get_baseline()

            if time.time() - start_time > runtime:
                running = False
                break

            sensor_data = sensor_stream.get_data(num_samples=1)
            if sensor_data:
                data = np.array(sensor_data[0].data) - baseline
                all_data.append(data)

                # Visualize the thumb sensor data
                for idx, chip_location in enumerate(chip_locations):
                    # Magnitude as red circles
                    data_magnitude = np.linalg.norm(data[idx * 3:(idx + 1) * 3]) / scaling
                    pygame.draw.circle(window, (255, 83, 72), chip_location, int(data_magnitude))

                    # Shear forces as green arrows
                    arrow_start = chip_location
                    rotation_mat = np.array([
                        [np.cos(chip_xy_rotations[idx]), -np.sin(chip_xy_rotations[idx])],
                        [np.sin(chip_xy_rotations[idx]), np.cos(chip_xy_rotations[idx])]
                    ])
                    data_xy = np.dot(rotation_mat, data[idx * 3: idx * 3 + 2])
                    data_xy[0] = -data_xy[0]
                    arrow_end = (
                        chip_location[0] + data_xy[0] / scaling,
                        chip_location[1] + data_xy[1] / scaling,
                    )
                    pygame.draw.line(window, (0, 255, 0), arrow_start, arrow_end, 2)

            pygame.display.flip()
            clock.tick(60)

    except KeyboardInterrupt:
        print("\nExiting visualization...")

    finally:
        pygame.quit()
        sensor_stream.pause_streaming()
        sensor_stream.join()
        if record and all_data:
            np.savetxt(filename, np.array(all_data))
            print(f"Data saved to {filename}")
        else:
            print("No data recorded to save.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Glove Visualization")
    parser.add_argument("-p", "--port", type=str, default=None, help="Serial port to connect to (e.g., /dev/ttyACM0 or /dev/cu.usbmodem101)")
    parser.add_argument("-v", "--viz_mode", type=str, default="graphic", help="Visualization mode (graphic or plot)")
    parser.add_argument("-b", "--baudrate", type=int, default=115200, help="Baud rate for serial communication")
    parser.add_argument("-r", "--runtime", type=int, default=180, help="Runtime for visualization (seconds)")
    args = parser.parse_args()

    port = get_val_port(args.port)

    print(f"Using port: {port}")

    if args.viz_mode == "graphic":
        graphic_visualizer(port, baudrate=args.baudrate, runtime=args.runtime)
    elif args.viz_mode == "plot":
        plot_visualizer(port, baudrate=args.baudrate, runtime=args.runtime)
