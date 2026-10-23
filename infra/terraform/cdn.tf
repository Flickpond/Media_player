# CloudFront in front of the bucket, for HLS segments: the traffic that grows
# with every viewer. Cached near the viewer, and inside CloudFront's free
# monthly allowance where S3 would bill every byte out.
#
# Private like the bucket: every request needs a CloudFront signed URL, which
# the API issues per segment after its ownership check
# (app/services/output_urls.py). The bucket admits this distribution, for
# outputs/ only -- originals are never served through it.

resource "tls_private_key" "cdn" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "aws_cloudfront_public_key" "cdn" {
  name        = "${local.name}-signer"
  comment     = "Verifies the API's signed segment URLs"
  encoded_key = tls_private_key.cdn.public_key_pem
}

resource "aws_cloudfront_key_group" "cdn" {
  name  = "${local.name}-signers"
  items = [aws_cloudfront_public_key.cdn.id]
}

resource "aws_cloudfront_origin_access_control" "videos" {
  name                              = "${local.name}-videos"
  description                       = "CloudFront signs its requests to the video bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# hls.js fetches segments with XHR from the site's page, so the responses need
# CORS headers. Added here rather than forwarded from S3: the cache key then
# need not include the request's Origin, and one cached copy serves everyone.
resource "aws_cloudfront_response_headers_policy" "cors" {
  name = "${local.name}-segments-cors"

  cors_config {
    access_control_allow_credentials = false
    origin_override                  = true
    access_control_max_age_sec       = 3600

    access_control_allow_headers {
      items = ["*"]
    }
    access_control_allow_methods {
      items = ["GET", "HEAD", "OPTIONS"]
    }
    access_control_allow_origins {
      items = var.site_origins
    }
  }
}

data "aws_cloudfront_cache_policy" "optimized" {
  # Leaves query strings out of the cache key, so the per-viewer signature
  # does not split the cache into one copy per URL.
  name = "Managed-CachingOptimized"
}

# --- our own name (sprint 6, D): cdn.flickpond.com -----------------------------
#
# Off while `cdn_alias` is null, and then nothing below exists and the
# distribution is exactly as before. On, the certificate is requested here
# and validated by a CNAME someone adds at Spaceship (the DNS is not in
# Route 53); the validation resource waits for it, so the distribution only
# takes the name once the certificate is issued.

locals {
  cdn_alias_on = var.cdn_alias != null
  # The name the API signs URLs for: ours once the distribution answers to it.
  cdn_signed_domain = local.cdn_alias_on ? var.cdn_alias : aws_cloudfront_distribution.videos.domain_name
}

resource "aws_acm_certificate" "cdn" {
  count    = local.cdn_alias_on ? 1 : 0
  provider = aws.us_east_1

  domain_name       = var.cdn_alias
  validation_method = "DNS"

  tags = {
    Name = "${local.name}-cdn"
  }

  # A changed name gets its new certificate before the old one is let go,
  # so the distribution is never left holding nothing.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "cdn" {
  count    = local.cdn_alias_on ? 1 : 0
  provider = aws.us_east_1

  certificate_arn = aws_acm_certificate.cdn[0].arn
  # No record FQDNs: they are made by hand at Spaceship, and this waits
  # (up to its timeout) for ACM to see them and issue.
}

# Accepted findings, each on purpose:
#   AVD-AWS-0010 access logging -- a logging bucket for a staging CDN; the
#     viewer-facing logs that matter are the API's, which issues every URL.
#   AVD-AWS-0011 WAF -- every request already needs a valid signature, which
#     a WAF rule could not tighten; a web ACL is a monthly charge for nothing.
# trivy:ignore:AVD-AWS-0010
# trivy:ignore:AVD-AWS-0011
resource "aws_cloudfront_distribution" "videos" {
  enabled         = true
  comment         = "${local.name}: HLS segments"
  is_ipv6_enabled = true
  http_version    = "http2and3"
  # North America, Europe and Asia -- where the viewers are -- without paying
  # for the most expensive edge locations.
  price_class = "PriceClass_200"
  aliases     = local.cdn_alias_on ? [var.cdn_alias] : null

  origin {
    origin_id                = "videos"
    domain_name              = aws_s3_bucket.videos.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.videos.id
  }

  default_cache_behavior {
    target_origin_id           = "videos"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = false # segments are already compressed video
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.cors.id
    trusted_key_groups         = [aws_cloudfront_key_group.cdn.id]
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # Without `cdn_alias`, the *.cloudfront.net certificate: AWS ignores the
  # minimum there and records TLSv1 whatever is asked, so asking for more only
  # made every plan show a change no apply could make (#88). With it, our own
  # certificate, and TLS 1.2 is enforced.
  viewer_certificate {
    cloudfront_default_certificate = !local.cdn_alias_on
    acm_certificate_arn            = local.cdn_alias_on ? aws_acm_certificate_validation.cdn[0].certificate_arn : null
    ssl_support_method             = local.cdn_alias_on ? "sni-only" : null
    minimum_protocol_version       = local.cdn_alias_on ? "TLSv1.2_2021" : "TLSv1"
  }
}

# The bucket admits this one distribution, for outputs only.
data "aws_iam_policy_document" "videos_from_cdn" {
  statement {
    sid       = "CloudFrontReadsOutputs"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.videos.arn}/outputs/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.videos.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "videos" {
  bucket = aws_s3_bucket.videos.id
  policy = data.aws_iam_policy_document.videos_from_cdn.json

  # The public-access block must be in place first, so this policy is never
  # evaluated against a bucket that could still be made public.
  depends_on = [aws_s3_bucket_public_access_block.videos]
}

# The API reads these at deploy time (deploy/aws/fetch-secrets.sh). The key is
# base64 so it fits on one line of .env.
resource "aws_ssm_parameter" "cdn_domain" {
  name  = "/flickpond/${var.environment}/cloudfront_domain"
  type  = "String"
  value = local.cdn_signed_domain
}

resource "aws_ssm_parameter" "cdn_key_pair_id" {
  name  = "/flickpond/${var.environment}/cloudfront_key_pair_id"
  type  = "String"
  value = aws_cloudfront_public_key.cdn.id
}

resource "aws_ssm_parameter" "cdn_private_key" {
  name        = "/flickpond/${var.environment}/cloudfront_private_key_b64"
  description = "Signs CloudFront URLs for HLS segments (base64 PEM)"
  type        = "SecureString"
  value       = base64encode(tls_private_key.cdn.private_key_pem)
}
