# Offline: every provider is mocked, so these run with no account and no
# credentials (Terraform 1.7+):
#
#   terraform init -backend=false && terraform test
#
# Only the CDN's own name (sprint 6, cdn.tf) is pinned here: off, the
# distribution must be exactly what production runs today; on, TLS 1.2 must
# actually be enforced and the API must sign for the name viewers reach.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }

  mock_data "aws_subnets" {
    defaults = {
      ids = ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"]
    }
  }

  mock_resource "aws_cloudfront_distribution" {
    defaults = {
      domain_name = "d111111abcdef8.cloudfront.net"
      arn         = "arn:aws:cloudfront::123456789012:distribution/E111111ABCDEF8"
    }
  }
}

mock_provider "aws" {
  alias = "us_east_1"

  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555"
    }
  }

  mock_resource "aws_acm_certificate_validation" {
    defaults = {
      certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555"
    }
  }
}

mock_provider "random" {}

# Long enough for the ElastiCache auth token's own validation, which a
# generated mock value is not.
override_resource {
  target = random_password.cache
  values = {
    result = "mockmockmockmockmockmockmockmock"
  }
}

mock_provider "tls" {}

run "without_an_alias_the_distribution_is_as_it_was" {
  command = apply

  assert {
    condition     = length(aws_acm_certificate.cdn) == 0 && length(aws_acm_certificate_validation.cdn) == 0
    error_message = "No certificate should exist until cdn_alias is set."
  }

  assert {
    condition     = length(coalesce(aws_cloudfront_distribution.videos.aliases, [])) == 0
    error_message = "The distribution should answer only to its cloudfront.net name."
  }

  assert {
    condition = (
      one(aws_cloudfront_distribution.videos.viewer_certificate).cloudfront_default_certificate
      && one(aws_cloudfront_distribution.videos.viewer_certificate).minimum_protocol_version == "TLSv1"
    )
    error_message = "Off, the viewer certificate must stay exactly as applied: anything else is a change every plan shows."
  }

  assert {
    condition     = output.cdn_signed_domain == "d111111abcdef8.cloudfront.net" && aws_ssm_parameter.cdn_domain.value == output.cdn_signed_domain
    error_message = "The API should keep signing for the cloudfront.net name."
  }
}

run "with_an_alias_tls_1_2_is_enforced_on_our_own_certificate" {
  command = apply

  variables {
    cdn_alias = "cdn.flickpond.com"
  }

  assert {
    condition     = aws_acm_certificate.cdn[0].domain_name == "cdn.flickpond.com" && aws_acm_certificate.cdn[0].validation_method == "DNS"
    error_message = "The certificate should be for the alias, validated by a DNS record."
  }

  assert {
    condition     = aws_cloudfront_distribution.videos.aliases == toset(["cdn.flickpond.com"])
    error_message = "The distribution should answer to cdn.flickpond.com."
  }

  assert {
    condition = (
      !one(aws_cloudfront_distribution.videos.viewer_certificate).cloudfront_default_certificate
      && one(aws_cloudfront_distribution.videos.viewer_certificate).acm_certificate_arn == aws_acm_certificate_validation.cdn[0].certificate_arn
      && one(aws_cloudfront_distribution.videos.viewer_certificate).ssl_support_method == "sni-only"
      && one(aws_cloudfront_distribution.videos.viewer_certificate).minimum_protocol_version == "TLSv1.2_2021"
    )
    error_message = "On our own certificate, served by SNI, with TLS 1.2 as the floor (#88)."
  }

  assert {
    condition     = startswith(aws_acm_certificate_validation.cdn[0].certificate_arn, "arn:aws:acm:us-east-1:")
    error_message = "CloudFront only takes certificates from us-east-1."
  }

  assert {
    condition     = output.cdn_signed_domain == "cdn.flickpond.com" && aws_ssm_parameter.cdn_domain.value == output.cdn_signed_domain
    error_message = "The API should sign for the name the distribution now answers to."
  }
}

run "a_url_is_not_a_cdn_alias" {
  command = plan

  variables {
    cdn_alias = "https://cdn.flickpond.com/"
  }

  expect_failures = [var.cdn_alias]
}
