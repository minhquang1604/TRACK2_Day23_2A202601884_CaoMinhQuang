# Stretch Goal 4 (GUIDE.md) — Terraform, WRITE-ONLY.
#
# Không thuộc phạm vi chấm điểm (RUBRIC.md). Không cần AWS account, không `terraform
# apply` — chỉ để đọc và đối chiếu với code thật đã chạy trong lab. Mục tiêu: chứng
# minh mình hiểu `aws_s3_bucket_replication_configuration` ánh xạ 1-1 với những gì
# state/snapshot.py (put/get/rpo) và MANIFEST.json ĐÃ LÀM bằng filesystem, khi lên
# một hạ tầng AWS thật.
#
# Ánh xạ theo README.md § Overview:
#   Region A / Region B  -> us-east-1 / us-west-2 (README.md dòng "Region A / Region B")
#   state/replicate.py --every 30 --backend fs -> aws_s3_bucket_replication_configuration
#   state/snapshot.py put()  -> S3 PutObject vào bucket nguồn -> replication engine tự bắn sang đích
#   state/snapshot.py get()  -> S3 GetObject từ bucket ĐÍCH (đọc bản đã replicate, không phải nguồn)
#   state/_replica/dr-artifacts/MANIFEST.json -> object "MANIFEST.json" trong cùng bucket,
#     replicate y hệt 2 object dữ liệu kia (KHÔNG có filter riêng — xem cảnh báo ở dưới)

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "primary_region" {
  description = "Vung 'Region A' trong lab — README.md: Region A = us-east-1"
  type        = string
  default     = "us-east-1"
}

variable "secondary_region" {
  description = "Vung 'Region B' trong lab — README.md: Region B = us-west-2"
  type        = string
  default     = "us-west-2"
}

variable "dr_bucket_name" {
  description = "Tuong ung DR_BUCKET trong state/snapshot.py (mac dinh 'dr-artifacts')"
  type        = string
  default     = "dr-artifacts"
}

provider "aws" {
  alias  = "primary"
  region = var.primary_region
}

provider "aws" {
  alias  = "secondary"
  region = var.secondary_region
}

# --- Bucket nguồn: nơi state/snapshot.py put() ghi vectors.sqlite + model.bin + MANIFEST.json ---
resource "aws_s3_bucket" "primary" {
  provider = aws.primary
  bucket   = "${var.dr_bucket_name}-${var.primary_region}"
}

# Replication trên S3 THẬT bắt buộc versioning bật ở CẢ HAI đầu — đây chính là thứ
# state/snapshot.py KHÔNG có: put() ghi đè file bằng shutil.copy2, không giữ lịch sử
# bản cũ. MANIFEST.json "versioning" trong lab chỉ là 1 file JSON bị ghi đè mỗi lần
# put() — không phải S3 object versioning thật. Đây là khoảng cách thật giữa mock và AWS.
resource "aws_s3_bucket_versioning" "primary" {
  provider = aws.primary
  bucket   = aws_s3_bucket.primary.id
  versioning_configuration {
    status = "Enabled"
  }
}

# --- Bucket đích: nơi state/snapshot.py get() đọc lại (khi failover.py restore) ---
resource "aws_s3_bucket" "replica" {
  provider = aws.secondary
  bucket   = "${var.dr_bucket_name}-${var.secondary_region}"
}

resource "aws_s3_bucket_versioning" "replica" {
  provider = aws.secondary
  bucket   = aws_s3_bucket.replica.id
  versioning_configuration {
    status = "Enabled"
  }
}

# --- IAM role cho replication engine — không có tương đương trong state/snapshot.py
# vì backend "fs" chạy local, không cần cross-account permission nào cả ---
resource "aws_iam_role" "replication" {
  provider = aws.primary
  name     = "${var.dr_bucket_name}-replication-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "replication" {
  provider = aws.primary
  name     = "${var.dr_bucket_name}-replication-policy"
  role     = aws_iam_role.replication.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
        Resource = [aws_s3_bucket.primary.arn]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObjectVersionForReplication",
          "s3:GetObjectVersionAcl",
          "s3:GetObjectVersionTagging",
        ]
        Resource = ["${aws_s3_bucket.primary.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags"]
        Resource = ["${aws_s3_bucket.replica.arn}/*"]
      },
    ]
  })
}

# --- Chính resource được yêu cầu ở Stretch Goal 4 ---
#
# CẢNH BÁO CỐ Ý (đây là bài học của §3 "backup index nhưng quên backup embedding
# model version -> index không tương thích khi restore"): rule dưới đây CHỦ ĐỘNG
# dùng filter prefix = "" (replicate MỌI object trong bucket) — KHÔNG lọc riêng
# "vectors.sqlite" hay "model.bin". Nếu ai đó "tối ưu" bằng cách thêm filter chỉ
# khớp "vectors*" để giảm băng thông, MANIFEST.json (chứa embed_model_version) sẽ
# KHÔNG được replicate — bản restore ở region phụ sẽ có vector DB mới nhưng
# embedding model version cũ/thiếu, đúng lỗi state/snapshot.py:get() sẽ gặp phải
# nếu MANIFEST.json không tồn tại (raise SystemExit ở snapshot.py dòng ~92).
resource "aws_s3_bucket_replication_configuration" "vectors_and_weights" {
  provider   = aws.primary
  depends_on = [aws_s3_bucket_versioning.primary, aws_s3_bucket_versioning.replica]

  role   = aws_iam_role.replication.arn
  bucket = aws_s3_bucket.primary.id

  rule {
    id     = "replicate-all-dr-artifacts" # vectors.sqlite + model.bin + MANIFEST.json cùng 1 rule
    status = "Enabled"

    filter {} # prefix rỗng có chủ đích — xem cảnh báo ở trên

    destination {
      bucket        = aws_s3_bucket.replica.arn
      storage_class = "STANDARD"

      # Replication Time Control = SLA 15 phút cho việc replicate xong. Đây chính là
      # con số nên thay cho `--every 30` cứng trong state/replicate.py nếu triển khai
      # thật — RPO mục tiêu của bạn phải >= SLA này, không thể nhỏ hơn.
      replication_time {
        status = "Enabled"
        time {
          minutes = 15
        }
      }
      metrics {
        status = "Enabled"
        event_threshold {
          minutes = 15
        }
      }
    }

    delete_marker_replication {
      status = "Disabled" # snapshot.py khong bao gio xoa MANIFEST.json, chi ghi de -> khong can replicate delete marker
    }
  }
}

output "rpo_theoretical_ceiling_seconds" {
  description = <<-EOT
    Trần lý thuyết của RPO khi dùng S3 CRR + Replication Time Control 15 phút:
    replication_time.minutes * 60. So sánh với RPO đo được thật trong
    reports/rto-evidence.md (bảng "Drill 2") — lab dùng --every 30 (giây) trong
    state/replicate.py nên RPO đo được NHỎ HƠN NHIỀU so với trần lý thuyết ở đây;
    con số 15 phút là SLA của AWS khi KHÔNG tự polling, khác hẳn với vòng lặp chủ
    động trong state/replicate.py.
  EOT
  value       = 15 * 60
}
