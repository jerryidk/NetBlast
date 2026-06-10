import json
import re
import subprocess

import matplotlib.pyplot as plt


def main():
    modes = ["maglev", "dramblast"]
    cores = list(range(1, 11))  # Cores 1 through 10

    # Dictionary to store all results
    results_data = {mode: {} for mode in modes}

    # Regex patterns for parsing output
    min_pattern = re.compile(r"Minimum:\s+(\d+)")
    max_pattern = re.compile(r"Maximum:\s+(\d+)")
    avg_pattern = re.compile(r"Average:\s+([0-9.]+)")

    print("Starting test runs...")

    # 1. Run the bash script and collect data
    for mode in modes:
        print(f"\n--- Running mode: {mode} ---")
        for core in cores:
            # Execute the command: ./run.sh <core> <mode>
            command = ["./run.sh", str(core), mode]
            print(f"Executing: {' '.join(command)}")

            try:
                # Run command and capture standard output
                process = subprocess.run(
                    command, capture_output=True, text=True, check=True
                )
                output = process.stdout

                # Parse the output using regex
                min_match = min_pattern.search(output)
                max_match = max_pattern.search(output)
                avg_match = avg_pattern.search(output)

                if min_match and max_match and avg_match:
                    min_val = float(min_match.group(1))
                    max_val = float(max_match.group(1))
                    avg_val = float(avg_match.group(1))

                    # Store in our dictionary
                    results_data[mode][core] = {
                        "min": min_val,
                        "max": max_val,
                        "avg": avg_val,
                    }
                    print(f"  -> Parsed: Min={min_val}, Max={max_val}, Avg={avg_val}")
                else:
                    print(
                        f"  -> Error: Could not parse output for core {core}. Output was:\n{output}"
                    )

            except subprocess.CalledProcessError as e:
                print(f"  -> Error executing command for core {core}: {e}")
            except Exception as e:
                print(f"  -> Unexpected error: {e}")

    # 2. Output the data to a JSON file
    json_filename = "results.json"
    with open(json_filename, "w") as json_file:
        json.dump(results_data, json_file, indent=4)
    print(f"\nResults saved to {json_filename}")

    # 3. Plot the data
    plt.figure(figsize=(10, 6))

    for mode in modes:
        # Extract data for plotting. Make sure the core was successfully parsed.
        valid_cores = [c for c in cores if c in results_data[mode]]

        if not valid_cores:
            continue

        avgs = [results_data[mode][c]["avg"] for c in valid_cores]
        mins = [results_data[mode][c]["min"] for c in valid_cores]
        maxs = [results_data[mode][c]["max"] for c in valid_cores]

        # Plot the average line
        line = plt.plot(
            valid_cores, avgs, marker="o", label=f"{mode} (Average)", linewidth=2
        )

        # Fill the area between min and max to indicate the range
        color = line[0].get_color()  # Match the fill color to the line color
        plt.fill_between(
            valid_cores,
            mins,
            maxs,
            color=color,
            alpha=0.2,
            label=f"{mode} (Min/Max Range)",
        )

    # Formatting the plot
    plt.title("Performance Stats by Core Count and Mode", fontsize=14)
    plt.xlabel("Number of Cores", fontsize=12)
    plt.ylabel("Performance Value", fontsize=12)
    plt.xticks(cores)  # Force x-axis to show only whole integer core counts
    plt.legend(loc="best")
    plt.grid(True, linestyle="--", alpha=0.6)

    # Save and display the plot
    plot_filename = "performance_plot.png"
    plt.savefig(plot_filename, dpi=300)
    print(f"Plot saved to {plot_filename}")
    plt.show()


if __name__ == "__main__":
    main()
