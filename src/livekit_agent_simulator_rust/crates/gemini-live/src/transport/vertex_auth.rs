use google_cloud_auth::credentials::{AccessTokenCredentials, Builder};

use super::{
    BearerTokenFuture, BearerTokenProvider, DynBearerTokenProvider, install_rustls_crypto_provider,
};
use crate::error::BearerTokenError;

const CLOUD_PLATFORM_SCOPE: &str = "https://www.googleapis.com/auth/cloud-platform";

/// Vertex AI bearer-token helper backed by Google Cloud Application Default
/// Credentials.
///
/// This helper follows the official ADC search order:
///
/// 1. `GOOGLE_APPLICATION_CREDENTIALS`
/// 2. The local ADC file created by
///    `gcloud auth application-default login`
/// 3. The attached service account from the metadata server
///
/// The helper requests the `cloud-platform` OAuth scope, which is what the
/// current Vertex AI auth docs show in their access-token examples.
#[derive(Clone, Debug)]
pub struct VertexAiApplicationDefaultCredentials {
    credentials: AccessTokenCredentials,
}

impl VertexAiApplicationDefaultCredentials {
    /// Create a new ADC-backed Vertex bearer-token helper.
    pub fn new() -> Result<Self, BearerTokenError> {
        install_rustls_crypto_provider();

        let credentials = Builder::default()
            .with_scopes([CLOUD_PLATFORM_SCOPE])
            .build_access_token_credentials()
            .map_err(|e| {
                BearerTokenError::with_source(
                    "failed to load Google Cloud Application Default Credentials",
                    e,
                )
            })?;

        Ok(Self { credentials })
    }

    /// Convert this helper into a transport-level bearer-token provider.
    pub fn into_bearer_token_provider(self) -> BearerTokenProvider {
        BearerTokenProvider::new(self)
    }
}

impl DynBearerTokenProvider for VertexAiApplicationDefaultCredentials {
    fn name(&self) -> &'static str {
        "vertex-ai-application-default"
    }

    fn bearer_token(&self) -> BearerTokenFuture<'_> {
        Box::pin(async move {
            install_rustls_crypto_provider();

            let access_token = self.credentials.access_token().await.map_err(|e| {
                BearerTokenError::with_source(
                    "failed to refresh Google Cloud access token from Application Default Credentials",
                    e,
                )
            })?;

            Ok(access_token.token)
        })
    }
}
