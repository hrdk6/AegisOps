// Package promclient is a minimal Prometheus instant-query client used for
// deterministic canary analysis.
package promclient

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Client queries the Prometheus HTTP API.
type Client struct {
	BaseURL string
	HTTP    *http.Client
}

// New returns a client with a bounded timeout.
func New(base string) *Client {
	return &Client{BaseURL: base, HTTP: &http.Client{Timeout: 5 * time.Second}}
}

type response struct {
	Status string `json:"status"`
	Error  string `json:"error"`
	Data   struct {
		ResultType string `json:"resultType"`
		Result     []struct {
			Value [2]any `json:"value"`
		} `json:"result"`
	} `json:"data"`
}

// Scalar evaluates query and returns the first sample. ok is false when the
// query returned no data or NaN.
func (c *Client) Scalar(ctx context.Context, query string) (value float64, ok bool, err error) {
	if c == nil || c.BaseURL == "" {
		return 0, false, fmt.Errorf("prometheus not configured")
	}
	u := c.BaseURL + "/api/v1/query?query=" + url.QueryEscape(query)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return 0, false, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return 0, false, err
	}
	defer resp.Body.Close()
	var r response
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return 0, false, fmt.Errorf("decode prometheus response: %w", err)
	}
	if r.Status != "success" {
		return 0, false, fmt.Errorf("prometheus error: %s", r.Error)
	}
	if len(r.Data.Result) == 0 {
		return 0, false, nil
	}
	s, _ := r.Data.Result[0].Value[1].(string)
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, false, err
	}
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return 0, false, nil
	}
	return v, true, nil
}
