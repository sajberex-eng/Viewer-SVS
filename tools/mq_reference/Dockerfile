# Эталон MarrowQuant 2.0 в настоящем ImageJ (tools/mq_reference/README.md).
# Команды ImageJ с параметрами идут через окна настроек, поэтому нужен виртуальный экран.
FROM eclipse-temurin:21-jdk
RUN apt-get update && apt-get install -y --no-install-recommends xvfb xauth libxrender1 libxtst6 libxi6 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /mq
