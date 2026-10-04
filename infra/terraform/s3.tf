# The bucket that replaces MinIO. Same object layout (uploads/, outputs/), so
# the application only changes where it points, not what it writes.

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# Accepted findings, each on purpose:
#   AVD-AWS-0090 versioning -- a user's delete must remove the video, not
#     leave a hidden noncurrent copy of it.
#   AVD-AWS-0089 access logging -- needs a second bucket; staging only.
#     Sprint 5, with the production cutover.
# trivy:ignore:AVD-AWS-0090
# trivy:ignore:AVD-AWS-0089
resource "aws_s3_bucket" "videos" {
  # Bucket names are global; the suffix keeps a re-created staging from
  # colliding with a name AWS has not released yet.
  bucket = "flickpond-${var.environment}-videos-${random_id.bucket_suffix.hex}"
}

# Nothing in this bucket is ever public. Browsers reach objects only through
# URLs the API signs after its ownership check -- the same rule as MinIO today.
resource "aws_s3_bucket_public_access_block" "videos" {
  bucket = aws_s3_bucket.videos.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "videos" {
  bucket = aws_s3_bucket.videos.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# SSE-S3, not a customer-managed KMS key (AVD-AWS-0132): with KMS every
# object read is a billed, rate-limited decrypt call, and HLS playback is
# hundreds of segment reads per video. Data is encrypted at rest either way.
# trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "videos" {
  bucket = aws_s3_bucket.videos.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "videos" {
  bucket = aws_s3_bucket.videos.id

  cors_rule {
    # PUT: direct multipart upload (contract 2.2). GET/HEAD: hls.js fetches
    # playlists and segments with XHR, which is a CORS request, unlike a
    # plain <video src>.
    allowed_methods = ["GET", "HEAD", "PUT"]
    allowed_origins = var.site_origins
    allowed_headers = ["*"]
    # Without this the browser hides each part's ETag from the page, and
    # `complete` cannot be called: S3 needs every part's ETag to assemble
    # the object. The failure shows up only at the very end of an upload.
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "videos" {
  bucket = aws_s3_bucket.videos.id

  rule {
    id     = "abort-abandoned-multipart-uploads"
    status = "Enabled"

    filter {}

    # A closed tab mid-upload leaves its parts behind, invisible to every
    # listing and billed as storage forever. The reaper cannot see them --
    # this is the only thing that removes them.
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}
