FROM ubuntu:latest

RUN apt update && apt install -y wget tar sudo git build-essential libbz2-dev libdb-dev \
    libreadline-dev libffi-dev libgdbm-dev liblzma-dev \
    libncursesw5-dev libsqlite3-dev libssl-dev \
    zlib1g-dev uuid-dev && wget https://www.python.org/ftp/python/3.11.4/Python-3.11.4.tgz \
    && tar -xf Python-3.11.4.tgz && cd Python-3.11.4 \
    && ./configure \
    && make \
    && sudo make install \
    && rm Python-3.11.4.tgz

COPY ./requirements.txt /tmp/
COPY ./driver/chromedriver /driver/

RUN python3 -m venv .venv \
    && ["/bin/bash", "-c", "source .venv/bin/activate"] \
    && pip3 install -r /tmp/requirements.txt
