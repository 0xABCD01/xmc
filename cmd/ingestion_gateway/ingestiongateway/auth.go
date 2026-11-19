// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package ingestiongateway

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"net/http"
	"os"
	"strings"

	"xmanager_cloud/internal/logging"
	"xmanager_cloud/pkg/auth/oauth2"

	"google.golang.org/api/idtoken"
)

// AuthMiddleware enforces Bearer token authentication.
type AuthMiddleware struct {
	Config    *Config
	Metrics   *GatewayMetrics
	TokenList []string
}

func NewAuthMiddleware(cfg *Config, metrics *GatewayMetrics) *AuthMiddleware {
	am := &AuthMiddleware{
		Config:  cfg,
		Metrics: metrics,
	}

	if cfg.AuthToken != "" {
		am.TokenList = parseCommaNewlineSeparated(cfg.AuthToken)
	}
	if cfg.AuthTokenFile != "" {
		if content, err := os.ReadFile(cfg.AuthTokenFile); err == nil {
			am.TokenList = append(am.TokenList, parseCommaNewlineSeparated(string(content))...)
		} else {
			logging.Error(context.Background(), "Failed to read AuthTokenFile", "error", err)
		}
	}

	return am
}

func parseCommaNewlineSeparated(s string) []string {
	var result []string
	for _, f := range strings.FieldsFunc(s, func(r rune) bool {
		return r == ',' || r == '\n' || r == '\r' || r == ' '
	}) {
		if f != "" {
			result = append(result, f)
		}
	}
	return result
}

func (am *AuthMiddleware) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !am.Config.RequireAuth && len(am.TokenList) == 0 && am.Config.JWTSecret == "" && !am.Config.EnableGoogleOIDC {
			next.ServeHTTP(w, r)
			return
		}

		authHeader := r.Header.Get("Authorization")
		if !strings.HasPrefix(authHeader, "Bearer ") {
			if am.Metrics != nil {
				am.Metrics.RecordAuthFailure("missing_token")
			}
			http.Error(w, "Unauthorized: missing or invalid Authorization header", http.StatusUnauthorized)
			return
		}

		tokenStr := strings.TrimPrefix(authHeader, "Bearer ")
		if am.validateToken(r.Context(), tokenStr) {
			next.ServeHTTP(w, r)
			return
		}

		if am.Metrics != nil {
			am.Metrics.RecordAuthFailure("invalid_token")
		}
		http.Error(w, "Unauthorized: invalid token", http.StatusUnauthorized)
	})
}

func (am *AuthMiddleware) validateToken(ctx context.Context, tokenStr string) bool {
	// 1. Check pre-shared static tokens
	tokenSha := sha256.Sum256([]byte(tokenStr))
	for _, t := range am.TokenList {
		tSha := sha256.Sum256([]byte(t))
		if subtle.ConstantTimeCompare(tokenSha[:], tSha[:]) == 1 {
			return true
		}
	}

	// 2. Check JWT
	if am.Config.JWTSecret != "" {
		if _, err := oauth2.ParseSessionToken(tokenStr, []byte(am.Config.JWTSecret), oauth2.CLIAudience, oauth2.FrontendAudience); err == nil {
			return true
		}
	}

	// 3. Check OIDC
	if am.Config.EnableGoogleOIDC && am.Config.OIDCAudience != "" {
		if payload, err := idtoken.Validate(ctx, tokenStr, am.Config.OIDCAudience); err == nil && payload != nil {
			return true
		}
	}
	return false
}
