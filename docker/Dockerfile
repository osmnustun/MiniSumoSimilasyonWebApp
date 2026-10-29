# Ubuntu 22.04 base with NVIDIA support
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    cmake \
    build-essential \
    python3 \
    python3-pip \
    python3-dev \
    curl \
    wget \
    pkg-config \
    libgl1-mesa-dev \
    libgl1-mesa-glx \
    libglu1-mesa-dev \
    libx11-dev \
    libxext-dev \
    libxrender-dev \
    libxrandr-dev \
    libxcursor-dev \
    libxinerama-dev \
    gcc-avr \
    binutils-avr \
    avr-libc \
    avrdude \
    simavr \
    libsimavr-dev \
    && rm -rf /var/lib/apt/lists/*

# Install arduino-cli
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh && \
    mv bin/arduino-cli /usr/local/bin/

# Configure and install the Arduino AVR core (for compiling .ino -> .elf/.hex)
RUN arduino-cli config init --overwrite && \
    arduino-cli config set board_manager.additional_urls "" && \
    arduino-cli core update-index && \
    arduino-cli core install arduino:avr

# Install Python packages
RUN pip3 install --no-cache-dir \
    numpy \
    python-multipart \
    pybullet \
    fastapi \
    uvicorn \
    pydantic \
    pyyaml \
    websockets \
    pillow

# Set up project directories
WORKDIR /app
COPY . /app

# Compile the AVR bridge harness (links against libsimavr)
RUN gcc -O2 -o /usr/local/bin/avr_harness \
        /app/bridge/avr_harness.c \
        $(pkg-config --cflags --libs simavr 2>/dev/null || echo "-I/usr/include/simavr -lsimavr") \
        -lm

# Setup NVIDIA EGL environment
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=all

# Expose FastAPI port
EXPOSE 7101

CMD ["python3", "-m", "uvicorn", "panel.main:app", "--host", "0.0.0.0", "--port", "7101", "--workers", "1"]
