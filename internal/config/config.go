package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// ConfigError mirrors config.py ConfigError: any load/validate failure.
type ConfigError struct{ msg string }

func (e *ConfigError) Error() string { return e.msg }

func errf(format string, args ...any) *ConfigError {
	return &ConfigError{msg: fmt.Sprintf(format, args...)}
}

var (
	envPattern      = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}`)
	sensitiveKey    = regexp.MustCompile(`(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key)`)
	placeholderToke = regexp.MustCompile(`\{([A-Za-z_][A-Za-z0-9_]*)\}`)
)

var customTransferPlaceholders = keySet(
	"artifact", "artifact_path", "filename", "sha256", "size_bytes",
	"remote_path", "dut_name", "dut_serial", "dut_address", "run_dir", "job_id",
)

var (
	selectionValues = keySet("newest", "largest", "fail-if-multiple")
	transferValues  = keySet("http", "scp", "tftp", "bootloader_tftp", "custom")
	parityValues    = keySet("none", "even", "odd", "mark", "space")
)

// Load reads, interpolates, strictly decodes, and validates a config file.
func Load(path string) (*OwrtConfig, error) {
	text, err := os.ReadFile(path)
	if err != nil {
		return nil, errf("cannot read config file %s: %v", path, err)
	}
	var raw any
	if err := yaml.Unmarshal(text, &raw); err != nil {
		return nil, errf("invalid YAML in %s: %v", path, err)
	}
	if raw == nil {
		raw = map[string]any{}
	}
	interp, err := interpolateEnv(raw)
	if err != nil {
		return nil, err
	}
	rebytes, err := yaml.Marshal(interp)
	if err != nil {
		return nil, errf("invalid config %s: %v", path, err)
	}
	cfg := Defaults()
	dec := yaml.NewDecoder(bytes.NewReader(rebytes))
	dec.KnownFields(true)
	if err := dec.Decode(&cfg); err != nil {
		return nil, errf("invalid config %s: %v", path, err)
	}
	if err := cfg.Validate(); err != nil {
		return nil, errf("invalid config %s: %v", path, err)
	}
	return &cfg, nil
}

func interpolateEnv(value any) (any, error) {
	switch v := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(v))
		for key, item := range v {
			r, err := interpolateEnv(item)
			if err != nil {
				return nil, err
			}
			out[key] = r
		}
		return out, nil
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			r, err := interpolateEnv(item)
			if err != nil {
				return nil, err
			}
			out[i] = r
		}
		return out, nil
	case string:
		return interpolateString(v)
	default:
		return value, nil
	}
}

func interpolateString(s string) (string, error) {
	matches := envPattern.FindAllStringSubmatchIndex(s, -1)
	if matches == nil {
		return s, nil
	}
	var b strings.Builder
	last := 0
	for _, m := range matches {
		b.WriteString(s[last:m[0]])
		name := s[m[2]:m[3]]
		hasDefault := m[4] != -1
		if val, ok := os.LookupEnv(name); ok {
			b.WriteString(val)
		} else if hasDefault {
			b.WriteString(s[m[4]:m[5]])
		} else {
			return "", errf("environment variable %s is required by config interpolation", name)
		}
		last = m[1]
	}
	b.WriteString(s[last:])
	return b.String(), nil
}

// ArtifactRoot resolves project.artifact_dir relative to the config file's
// directory (config.py OwrtConfig.artifact_root).
func (c *OwrtConfig) ArtifactRoot(configPath string) string {
	return resolvePath(c.Project.ArtifactDir, filepath.Dir(configPath))
}

// StateDBPath resolves the SQLite path (config.py OwrtConfig.state_db_path).
func (c *OwrtConfig) StateDBPath(configPath string) string {
	if c.Project.StateDB != nil && *c.Project.StateDB != "" {
		return resolvePath(*c.Project.StateDB, filepath.Dir(configPath))
	}
	return filepath.Join(c.ArtifactRoot(configPath), "owrt_monitor.sqlite3")
}

func resolvePath(p, baseDir string) string {
	if filepath.IsAbs(p) {
		return p
	}
	abs, err := filepath.Abs(filepath.Join(baseDir, p))
	if err != nil {
		return filepath.Join(baseDir, p)
	}
	return abs
}

// EffectiveProfile returns the explicit profile or the config default.
func (c *OwrtConfig) EffectiveProfile(requested *string) *string {
	if requested != nil {
		return requested
	}
	return c.Project.DefaultProfile
}

// ListProfiles returns the defined profile names, sorted.
func (c *OwrtConfig) ListProfiles() []string {
	names := make([]string, 0, len(c.Profiles))
	for name := range c.Profiles {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// RedactedDump returns a JSON-shaped map with secrets masked
// (config.py OwrtConfig.redacted_dump). Safe for snapshots, reports, logs.
func (c *OwrtConfig) RedactedDump() (map[string]any, error) {
	data, err := json.Marshal(c)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	if dut, ok := out["dut"].(map[string]any); ok {
		if login, ok := dut["login"].(map[string]any); ok {
			if pw, _ := login["password"].(string); pw != "" {
				login["password"] = "<redacted>"
			}
		}
	}
	if builder, ok := out["builder"].(map[string]any); ok {
		if env, ok := builder["env"].(map[string]any); ok {
			for key := range env {
				if sensitiveKey.MatchString(key) {
					env[key] = "<redacted>"
				}
			}
		}
	}
	return out, nil
}

// WithProfile deep-merges the named profile overlay onto the base config and
// re-validates (config.py OwrtConfig.with_profile). Lists/scalars replace
// wholesale; nested maps merge key-by-key.
func (c *OwrtConfig) WithProfile(name string) (*OwrtConfig, error) {
	overlay, ok := c.Profiles[name]
	if !ok {
		avail := strings.Join(c.ListProfiles(), ", ")
		if avail == "" {
			avail = "(no profiles defined)"
		}
		return nil, errf("unknown profile %q; available: %s", name, avail)
	}
	// Base = full dump without profiles, with default_profile cleared.
	data, err := yaml.Marshal(c)
	if err != nil {
		return nil, errf("applying profile %q produced an invalid config: %v", name, err)
	}
	var base map[string]any
	if err := yaml.Unmarshal(data, &base); err != nil {
		return nil, errf("applying profile %q produced an invalid config: %v", name, err)
	}
	delete(base, "profiles")
	if project, ok := base["project"].(map[string]any); ok {
		project["default_profile"] = nil
	}
	merged := deepMerge(base, toAnyMap(overlay))
	rebytes, err := yaml.Marshal(merged)
	if err != nil {
		return nil, errf("applying profile %q produced an invalid config: %v", name, err)
	}
	out := Defaults()
	dec := yaml.NewDecoder(bytes.NewReader(rebytes))
	dec.KnownFields(true)
	if err := dec.Decode(&out); err != nil {
		return nil, errf("applying profile %q produced an invalid config: %v", name, err)
	}
	out.Profiles = map[string]map[string]any{}
	if err := out.Validate(); err != nil {
		return nil, errf("applying profile %q produced an invalid config: %v", name, err)
	}
	return &out, nil
}

func toAnyMap(m map[string]any) map[string]any { return m }

func deepMerge(base, overlay map[string]any) map[string]any {
	result := make(map[string]any, len(base))
	for k, v := range base {
		result[k] = v
	}
	for k, ov := range overlay {
		if bv, ok := result[k].(map[string]any); ok {
			if ovm, ok := ov.(map[string]any); ok {
				result[k] = deepMerge(bv, ovm)
				continue
			}
		}
		result[k] = ov
	}
	return result
}
