package ingestiongateway

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"xmanager_cloud/pkg/auth/oauth2"
)

func TestAuthMiddleware(t *testing.T) {
	jwtSecret := "my-secret-that-must-be-long-enough-for-hs256"
	token, _ := oauth2.GenerateSessionToken("user@example.com", "Test User", "", time.Hour, []byte(jwtSecret))

	tests := []struct {
		name       string
		reqAuth    bool
		secret     string
		authHeader string
		wantCode   int
	}{
		{"disabled", false, "", "", http.StatusOK},
		{"missing token when required", true, jwtSecret, "", http.StatusUnauthorized},
		{"valid token", true, jwtSecret, "Bearer " + token, http.StatusOK},
		{"invalid token", true, jwtSecret, "Bearer bogus", http.StatusUnauthorized},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cfg := &Config{RequireAuth: tc.reqAuth, JWTSecret: tc.secret}
			am := NewAuthMiddleware(cfg, nil)
			
			req := httptest.NewRequest("GET", "/", nil)
			if tc.authHeader != "" {
				req.Header.Set("Authorization", tc.authHeader)
			}
			
			rr := httptest.NewRecorder()
			handler := am.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(http.StatusOK)
			}))
			
			handler.ServeHTTP(rr, req)
			
			if status := rr.Code; status != tc.wantCode {
				t.Errorf("handler returned wrong status code: got %v want %v", status, tc.wantCode)
			}
		})
	}
}
