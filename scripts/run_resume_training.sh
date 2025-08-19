#!/bin/bash

CHECKPOINT_PATH=""
DATA_FILE=""
DATA_FOLDER=""
BATCH_SIZE=32
EPOCHS=100
CACHE_DIR="cache"

if [ $# -lt 3 ]; then
    echo "Usage: $0 <checkpoint_path> <data_file> <data_folder> [batch_size] [total_epochs]"
    echo ""
    echo "Example:"
    echo "  $0 checkpoints/optimized_model_epoch_10_recall_0.3456.pt /path/to/annotations /path/to/images 32 100"
    exit 1
fi

CHECKPOINT_PATH=$1
DATA_FILE=$2
DATA_FOLDER=$3

if [ $# -ge 4 ]; then
    BATCH_SIZE=$4
fi

if [ $# -ge 5 ]; then
    EPOCHS=$5
fi

if [ ! -f "$CHECKPOINT_PATH" ]; then
    echo "❌ Checkpoint file not found: $CHECKPOINT_PATH"
    exit 1
fi

if [ ! -d "$DATA_FILE" ]; then
    echo "❌ Data file directory not found: $DATA_FILE"
    exit 1
fi

if [ ! -d "$DATA_FOLDER" ]; then
    echo "❌ Data folder not found: $DATA_FOLDER"
    exit 1
fi

echo "🚀 Starting Resume Training..."
echo "================================"
echo "Checkpoint: $CHECKPOINT_PATH"
echo "Data file: $DATA_FILE"
echo "Data folder: $DATA_FOLDER"
echo "Batch size: $BATCH_SIZE"
echo "Total epochs: $EPOCHS"
echo "Cache dir: $CACHE_DIR"
echo "================================"

mkdir -p $CACHE_DIR

python resume_training.py \
    --checkpoint "$CHECKPOINT_PATH" \
    --path-file "$DATA_FILE" \
    --path-folder "$DATA_FOLDER" \
    --batch $BATCH_SIZE \
    --epochs $EPOCHS \
    --cache-dir "$CACHE_DIR" \
    --gradient-accumulation 2 \
    --mixed-precision \
    --fast-validation

echo "✅ Resume training completed!"