import os
import time
from argparse import ArgumentParser

import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.cuda.amp import GradScaler, autocast
from torch.utils.data import DataLoader
from torchvision import transforms

from datasets.cached_fashioniq_dataset import CachedFashionDataset
from modules.fashion_vlp import FashionVLP


def batch_classification_loss(f_ref, f_tar, temperature=0.07):
    """
    Triển khai hàm loss từ phương trình (6) trong bài báo.
    Đây là loss tương phản (contrastive loss) InfoNCE.
    """
    B, D = f_ref.shape
    f_ref = F.normalize(f_ref, p=2, dim=1)
    f_tar = F.normalize(f_tar, p=2, dim=1)
    sim_matrix = torch.matmul(f_ref, f_tar.t()) / temperature
    labels = torch.arange(B, device=f_ref.device)
    loss = F.cross_entropy(sim_matrix, labels)
    return loss


def recall_at_k_from_sim(similarity_matrix: torch.Tensor, k: int = 10) -> float:
    """
    similarity_matrix: (N_query x N_gallery), đã được chuẩn hoá theo cosine (hoặc dot)
    giả định cặp đúng của query i là gallery i (vì ta append theo cùng thứ tự).
    """
    Nq, Ng = similarity_matrix.shape
    k = min(k, Ng)
    topk_idx = similarity_matrix.topk(k, dim=1).indices  # (Nq x k)
    gt = torch.arange(Nq, device=similarity_matrix.device).view(-1, 1)  # (Nq x 1)
    correct = (topk_idx == gt).any(dim=1).float().mean().item()
    return correct


def evaluate_model(model, val_loader, device, k=10, max_batches=None, chunk_size=None):
    """
    Đánh giá R@K trên TOÀN BỘ gallery (không phải trong-batch).
    - Nếu 'chunk_size' được set (ví dụ 2048), sẽ tính theo từng khúc để tiết kiệm RAM.
    """
    model.eval()
    all_ref, all_tar = [], []

    with torch.no_grad():
        for batch_idx, batch in enumerate(val_loader):
            if max_batches is not None and batch_idx >= max_batches:
                break

            ref_images = batch["reference_image"].to(device, non_blocking=True)
            target_images = batch["target_image"].to(device, non_blocking=True)
            feedback_tokens = batch["feedback_tokens"].to(device, non_blocking=True)
            crop_reference_images = batch["crop_reference_image"].to(
                device, non_blocking=True
            )
            crop_target_images = batch["crop_target_image"].to(
                device, non_blocking=True
            )
            landmark_locations = batch["landmarks"]

            f_ref, f_tar = model(
                ref_images,
                feedback_tokens,
                target_images,
                crop_reference_images,
                crop_target_images,
                landmark_locations,
            )
            all_ref.append(f_ref)  # vẫn trên GPU để tính nhanh
            all_tar.append(f_tar)

    if len(all_ref) == 0:
        return 0.0

    f_ref_all = F.normalize(torch.cat(all_ref, dim=0), p=2, dim=1)  # (Nq x D)
    f_tar_all = F.normalize(torch.cat(all_tar, dim=0), p=2, dim=1)  # (Ng x D)

    # Tính full-matrix nếu vừa RAM
    if chunk_size is None:
        sim = f_ref_all @ f_tar_all.t()  # (Nq x Ng)
        return recall_at_k_from_sim(sim, k)

    # Hoặc tính theo từng khúc query để tiết kiệm RAM
    Nq = f_ref_all.size(0)
    recalls = []
    for start in range(0, Nq, chunk_size):
        end = min(start + chunk_size, Nq)
        sim_chunk = f_ref_all[start:end] @ f_tar_all.t()  # (chunk x Ng)
        recalls.append(recall_at_k_from_sim(sim_chunk, k) * (end - start))
    return sum(recalls) / Nq


def load_checkpoint(checkpoint_path, model, optimizer=None, scheduler=None, device='cpu'):
    """
    Load checkpoint và khôi phục training state
    
    Args:
        checkpoint_path: đường dẫn đến checkpoint file
        model: model instance
        optimizer: optimizer instance (optional)
        scheduler: scheduler instance (optional) 
        device: device để load checkpoint
        
    Returns:
        dict: thông tin checkpoint đã load
    """
    print(f"🔄 Loading checkpoint from: {checkpoint_path}")
    
    if not os.path.exists(checkpoint_path):
        raise FileNotFoundError(f"Checkpoint not found: {checkpoint_path}")
    
    # Load checkpoint
    checkpoint = torch.load(checkpoint_path, map_location=device)
    
    # Load model state
    model.load_state_dict(checkpoint["model_state_dict"])
    print("✅ Model state loaded")
    
    # Load optimizer state nếu có
    if optimizer is not None and "optimizer_state_dict" in checkpoint:
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        print("✅ Optimizer state loaded")
    
    # Load scheduler state nếu có
    if scheduler is not None and "scheduler_state_dict" in checkpoint:
        scheduler.load_state_dict(checkpoint["scheduler_state_dict"])
        print("✅ Scheduler state loaded")
    
    # In thông tin checkpoint
    print(f"📊 Checkpoint info:")
    print(f"  Epoch: {checkpoint.get('epoch', 'N/A')}")
    print(f"  Loss: {checkpoint.get('loss', 'N/A'):.4f}")
    print(f"  Recall@10: {checkpoint.get('recall_at_10', 'N/A'):.4f}")
    
    if 'train_losses' in checkpoint:
        print(f"  Training history: {len(checkpoint['train_losses'])} epochs")
    if 'val_recalls' in checkpoint:
        print(f"  Validation history: {len(checkpoint['val_recalls'])} epochs")
    
    return checkpoint


def main():
    parser = ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=str, help="Path to checkpoint file")
    parser.add_argument("--batch", default=64, type=int)
    parser.add_argument("--path-file", required=True, type=str)
    parser.add_argument("--path-folder", required=True, type=str)
    parser.add_argument(
        "--gradient-accumulation",
        default=2,
        type=int,
        help="Gradient accumulation steps",
    )
    parser.add_argument(
        "--mixed-precision", action="store_true", help="Enable mixed precision training"
    )
    parser.add_argument(
        "--epochs", default=100, type=int, help="Total number of training epochs"
    )
    parser.add_argument(
        "--cache-dir",
        default="cache",
        type=str,
        help="Cache directory for preprocessing",
    )
    parser.add_argument(
        "--fast-validation",
        action="store_true",
        help="Use subset of validation for faster evaluation",
    )
    parser.add_argument(
        "--lr", default=None, type=float, help="Override learning rate (optional)"
    )
    args = parser.parse_args()

    print("=== RESUME FASHION VLP TRAINING ===")
    print(f"Checkpoint: {args.checkpoint}")
    print(f"Batch size: {args.batch}")
    print(f"Gradient accumulation: {args.gradient_accumulation}")
    print(f"Effective batch size: {args.batch * args.gradient_accumulation}")
    print(f"Mixed precision: {args.mixed_precision}")
    print(f"Fast validation: {args.fast_validation}")
    print("=" * 40)

    # Data transforms
    image_transform = transforms.Compose(
        [
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ]
    )

    # Datasets with caching
    print("Khởi tạo datasets với caching...")
    start_time = time.time()

    train_dataset = CachedFashionDataset(
        annotations_folder=args.path_file,
        folder_img=args.path_folder,
        transform=image_transform,
        type="train",
        cache_dir=args.cache_dir,
    )

    val_dataset = CachedFashionDataset(
        annotations_folder=args.path_file,
        folder_img=args.path_folder,
        transform=image_transform,
        type="val",
        cache_dir=args.cache_dir,
    )

    print(f"Dataset initialization took: {time.time() - start_time:.2f}s")

    # Optimized DataLoaders
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch,
        shuffle=True,
        num_workers=4,
        pin_memory=True,
        persistent_workers=True,
        prefetch_factor=2,
    )

    val_loader = DataLoader(
        val_dataset,
        batch_size=args.batch,
        shuffle=False,
        num_workers=4,
        pin_memory=True,
        persistent_workers=True,
        prefetch_factor=2,
    )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    # Model setup
    model = FashionVLP().to(device)

    # Compile model for PyTorch 2.0+
    if hasattr(torch, "compile"):
        try:
            model = torch.compile(model)
            print("✅ Model compiled for faster training!")
        except Exception as e:
            print(f"⚠️ Model compilation failed: {e}")

    # Optimizer with weight decay
    lr = args.lr if args.lr is not None else 4e-4
    optimizer = optim.AdamW(
        model.parameters(), lr=lr, betas=(0.9, 0.999), weight_decay=0.01
    )

    # Learning rate scheduler
    scheduler = optim.lr_scheduler.OneCycleLR(
        optimizer,
        max_lr=3e-4,
        total_steps=args.epochs * len(train_loader) // args.gradient_accumulation,
        pct_start=0.1,
        anneal_strategy="cos",
    )

    # Mixed precision setup
    scaler = GradScaler() if args.mixed_precision else None

    # Load checkpoint
    checkpoint = load_checkpoint(args.checkpoint, model, optimizer, scheduler, device)
    
    # Khôi phục training state
    start_epoch = checkpoint.get('epoch', 0)
    train_losses = checkpoint.get('train_losses', [])
    val_recalls = checkpoint.get('val_recalls', [])
    best_recall = max(val_recalls) if val_recalls else 0.0
    
    print(f"\n🚀 Resuming training from epoch {start_epoch}...")
    print(f"📈 Previous best recall: {best_recall:.4f}")
    
    # Điều chỉnh scheduler nếu cần
    if 'scheduler_state_dict' not in checkpoint:
        print("⚠️ Scheduler state not found, reinitializing...")
        # Tính toán lại scheduler cho remaining epochs
        remaining_epochs = args.epochs - start_epoch
        scheduler = optim.lr_scheduler.OneCycleLR(
            optimizer,
            max_lr=3e-4,
            total_steps=remaining_epochs * len(train_loader) // args.gradient_accumulation,
            pct_start=0.1,
            anneal_strategy="cos",
        )

    print(f"✅ Mixed Precision Training: {'Enabled' if args.mixed_precision else 'Disabled'}")
    print(f"✅ Gradient Accumulation Steps: {args.gradient_accumulation}")

    # Training variables
    num_epochs = args.epochs
    best_model_path = None

    print(f"\n🔄 Continuing training from epoch {start_epoch + 1} to {num_epochs}...")
    training_start = time.time()

    for epoch in range(start_epoch, num_epochs):
        model.train()
        total_loss = 0
        num_batches = 0
        epoch_start = time.time()

        for batch_idx, batch in enumerate(train_loader):
            # Data loading với non_blocking
            ref_images = batch["reference_image"].to(device, non_blocking=True)
            target_images = batch["target_image"].to(device, non_blocking=True)
            feedback_tokens = batch["feedback_tokens"].to(device, non_blocking=True)
            crop_reference_images = batch["crop_reference_image"].to(
                device, non_blocking=True
            )
            crop_target_images = batch["crop_target_image"].to(
                device, non_blocking=True
            )
            landmark_locations = batch["landmarks"]

            # Forward pass with mixed precision
            if args.mixed_precision:
                with autocast():
                    f_ref, f_tar = model(
                        ref_images,
                        feedback_tokens,
                        target_images,
                        crop_reference_images,
                        crop_target_images,
                        landmark_locations,
                    )
                    loss = batch_classification_loss(f_ref, f_tar)
            else:
                f_ref, f_tar = model(
                    ref_images,
                    feedback_tokens,
                    target_images,
                    crop_reference_images,
                    crop_target_images,
                    landmark_locations,
                )
                loss = batch_classification_loss(f_ref, f_tar)

            # Scale loss for gradient accumulation
            loss = loss / args.gradient_accumulation

            # Backward pass
            if args.mixed_precision:
                scaler.scale(loss).backward()
            else:
                loss.backward()

            # Update weights after accumulating gradients
            if (batch_idx + 1) % args.gradient_accumulation == 0:
                if args.mixed_precision:
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    optimizer.step()
                optimizer.zero_grad()
                scheduler.step()

            total_loss += loss.item() * args.gradient_accumulation
            num_batches += 1

        avg_loss = total_loss / num_batches
        train_losses.append(avg_loss)
        epoch_time = time.time() - epoch_start

        print(f"Epoch [{epoch + 1}/{num_epochs}] completed in {epoch_time:.2f}s")
        print(f"  Training Loss: {avg_loss:.4f}")

        # Validation
        print(f"\n--- Validation tại Epoch {epoch + 1} ---")
        val_start = time.time()

        # Fast validation nếu được enable
        max_val_batches = 30 if args.fast_validation else None
        val_recall_10 = evaluate_model(
            model, val_loader, device, k=10, max_batches=max_val_batches
        )
        val_recalls.append(val_recall_10)

        val_time = time.time() - val_start
        print(f"  Validation R@10: {val_recall_10:.4f} (took {val_time:.2f}s)")
        print("-" * 50)

        # Save checkpoint
        os.makedirs("checkpoints", exist_ok=True)
        checkpoint_path = f"checkpoints/resumed_model_epoch_{epoch + 1}_recall_{val_recall_10:.4f}.pt"

        torch.save(
            {
                "epoch": epoch + 1,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "scheduler_state_dict": scheduler.state_dict(),
                "loss": avg_loss,
                "recall_at_10": val_recall_10,
                "train_losses": train_losses,
                "val_recalls": val_recalls,
            },
            checkpoint_path,
        )

        print(f"✅ Checkpoint saved: {checkpoint_path}")

        # Save best model
        if val_recall_10 > best_recall:
            best_recall = val_recall_10
            best_model_path = (
                f"checkpoints/best_resumed_model_recall_{best_recall:.4f}.pt"
            )
            torch.save(
                {
                    "epoch": epoch + 1,
                    "model_state_dict": model.state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                    "scheduler_state_dict": scheduler.state_dict(),
                    "loss": avg_loss,
                    "recall_at_10": val_recall_10,
                    "train_losses": train_losses,
                    "val_recalls": val_recalls,
                },
                best_model_path,
            )
            print(f"🏆 Best model updated: {best_model_path}")

    training_time = time.time() - training_start
    print(f"\n🎉 Resumed training completed in {training_time / 3600:.2f} hours!")

    # Final evaluation
    print("\n" + "=" * 60)
    print("FINAL EVALUATION")
    print("=" * 60)
    final_recall_10 = evaluate_model(model, val_loader, device, k=10)
    print(f"Final Validation R@10: {final_recall_10:.4f}")

    if val_recalls:
        best_recall_training = max(val_recalls)
        print(f"Best R@10 during training: {best_recall_training:.4f}")

    print(f"Final Training Loss: {train_losses[-1]:.4f}")

    # Save final model
    final_model_path = f"checkpoints/final_resumed_model_epoch_{num_epochs}_recall_{final_recall_10:.4f}.pt"
    torch.save(
        {
            "epoch": num_epochs,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "scheduler_state_dict": scheduler.state_dict(),
            "final_loss": train_losses[-1],
            "final_recall_at_10": final_recall_10,
            "train_losses": train_losses,
            "val_recalls": val_recalls,
            "best_recall": best_recall,
            "training_time": training_time,
        },
        final_model_path,
    )

    print(f"✅ Final model saved: {final_model_path}")

    if best_model_path:
        print(f"🏆 Best model: {best_model_path}")


if __name__ == "__main__":
    main()