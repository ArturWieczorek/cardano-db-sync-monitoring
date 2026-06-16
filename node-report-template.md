Environment Name - Mainnet
database name - cardano-node-mainnet-11.0.1

In Memory version

CPU & RAM (RSS)

- slot-axis
cardano-node-mainnet-11.0.1-slot-axis.png

- time-axis
cardano-node-mainnet-11.0.1-time-axis.png


Ingest Metrics

- epoch-axis
cardano-node-mainnet-11.0.1-ingest-metrics-epoch-axis.png


On-disk DB Size

- slot-axis
cardano-node-mainnet-11.0.1-disk-slot-axis.png

- time-axis
cardano-node-mainnet-11.0.1-disk-time-axis.png


RTS / Runtime Metrics

- slot-axis
cardano-node-mainnet-11.0.1-rts-slot-axis.png

- time-axis
cardano-node-mainnet-11.0.1-rts-time-axis.png


LSM version

CPU & RAM (RSS)

- slot-axis
cardano-node-mainnet-LSM-11.0.1-slot-axis.png

- time-axis
cardano-node-mainnet-LSM-11.0.1-time-axis.png


Ingest Metrics

- epoch-axis
cardano-node-mainnet-LSM-11.0.1-ingest-metrics-epoch-axis.png


On-disk DB Size

- slot-axis
cardano-node-mainnet-LSM-11.0.1-disk-slot-axis.png

- time-axis
cardano-node-mainnet-LSM-11.0.1-disk-time-axis.png


RTS / Runtime Metrics

- slot-axis
cardano-node-mainnet-LSM-11.0.1-rts-slot-axis.png

- time-axis
cardano-node-mainnet-LSM-11.0.1-rts-time-axis.png


LSM vs InMemory Comparison

(slot axis for CPU & RAM and On-disk Size, epoch axis for Ingest)

cardano-node-mainnet-LSM-vs-InMemory-slot-axis.png
cardano-node-mainnet-LSM-vs-InMemory-ingest-metrics-epoch-axis.png
cardano-node-mainnet-LSM-vs-InMemory-disk-slot-axis.png

Note: On-disk Size and RTS sections appear only when those optional collectors
(node-db-size-monitor.py / node-rts-monitor.py) recorded samples for the build.
