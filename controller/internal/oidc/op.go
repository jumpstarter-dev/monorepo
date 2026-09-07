package oidc

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/sha256"
	"encoding/binary"
	"math/rand"
	"time"

	"filippo.io/keygen"
	"github.com/gin-gonic/gin"
	"github.com/go-jose/go-jose/v4"
	"github.com/golang-jwt/jwt/v5"
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"github.com/zitadel/oidc/v3/pkg/op"
)

const defaultTokenLifetime = 365 * 24 * time.Hour

type Signer struct {
	privatekey    *ecdsa.PrivateKey
	issuer        string
	audience      string
	tokenLifetime time.Duration
}

func NewSigner(privateKey *ecdsa.PrivateKey, issuer, audience string) *Signer {
	return &Signer{
		privatekey: privateKey,
		issuer:     issuer,
		audience:   audience,
	}
}

func NewSignerFromSeed(seed []byte, issuer, audience string) (*Signer, error) {
	hash := sha256.Sum256(seed)
	source := rand.NewSource(int64(binary.BigEndian.Uint64(hash[:8])))
	reader := rand.New(source)
	key, err := keygen.ECDSALegacy(elliptic.P256(), reader)
	if err != nil {
		return nil, err
	}
	return NewSigner(key, issuer, audience), nil
}

func (k *Signer) Issuer() string {
	return k.issuer
}

func (k *Signer) Audience() string {
	return k.audience
}

func (k *Signer) ID() string {
	return "default"
}

func (k *Signer) Algorithm() jose.SignatureAlgorithm {
	return jose.ES256
}

func (k *Signer) Use() string {
	return "sig"
}

func (k *Signer) Key() any {
	return k.privatekey.Public()
}

func (k *Signer) KeySet(context.Context) ([]op.Key, error) {
	return []op.Key{k}, nil
}

func (k *Signer) Register(group gin.IRoutes) {
	group.GET("/.well-known/openid-configuration", func(c *gin.Context) {
		op.Discover(c.Writer, &oidc.DiscoveryConfiguration{
			Issuer:  k.issuer,
			JwksURI: k.issuer + "/jwks",
		})
	})

	group.GET("/jwks", func(c *gin.Context) {
		op.Keys(c.Writer, c.Request, k)
	})
}

func (k *Signer) SetTokenLifetime(d time.Duration) {
	k.tokenLifetime = d
}

func (k *Signer) Validate(token string) error {
	_, err := jwt.Parse(token, func(t *jwt.Token) (any, error) {
		return &k.privatekey.PublicKey, nil
	},
		jwt.WithValidMethods([]string{
			jwt.SigningMethodES256.Alg(),
		}),
		jwt.WithIssuer(k.issuer),
		jwt.WithAudience(k.audience),
	)
	return err
}

// ParseSubject validates the token and returns the subject claim.
func (k *Signer) ParseSubject(token string) (string, error) {
	claims := &jwt.RegisteredClaims{}
	_, err := jwt.ParseWithClaims(token, claims, func(t *jwt.Token) (any, error) {
		return &k.privatekey.PublicKey, nil
	},
		jwt.WithValidMethods([]string{
			jwt.SigningMethodES256.Alg(),
		}),
		jwt.WithIssuer(k.issuer),
		jwt.WithAudience(k.audience),
	)
	if err != nil {
		return "", err
	}
	return claims.Subject, nil
}

func (k *Signer) TokenExpiry(tokenString string) (time.Time, error) {
	parser := jwt.NewParser(jwt.WithoutClaimsValidation())
	claims := &jwt.RegisteredClaims{}
	if _, _, err := parser.ParseUnverified(tokenString, claims); err != nil {
		return time.Time{}, err
	}
	if claims.ExpiresAt == nil {
		return time.Time{}, nil
	}
	return claims.ExpiresAt.Time, nil
}

func (k *Signer) Token(
	subject string,
) (string, error) {
	lifetime := k.tokenLifetime
	if lifetime == 0 {
		lifetime = defaultTokenLifetime
	}
	now := time.Now()
	return jwt.NewWithClaims(jwt.SigningMethodES256, jwt.RegisteredClaims{
		Issuer:    k.issuer,
		Subject:   subject,
		Audience:  []string{k.audience},
		IssuedAt:  jwt.NewNumericDate(now),
		ExpiresAt: jwt.NewNumericDate(now.Add(lifetime)),
	}).SignedString(k.privatekey)
}
