/*
Copyright 2025. The Jumpstarter Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package log

import (
	"github.com/go-logr/logr"
)

// Log levels for consistent verbosity across the codebase
// These levels follow the logr convention where higher numbers indicate more verbosity
const (
	// LevelError represents error level logging (level 0, always shown)
	LevelError = 0

	// LevelWarning represents warning level logging (level 1)
	LevelWarning = 1

	// LevelInfo represents info level logging (level 2)
	LevelInfo = 2

	// LevelDebug represents debug level logging (level 3)
	LevelDebug = 3

	// LevelTrace represents trace level logging (level 4)
	LevelTrace = 4

	// LevelVerbose represents very verbose trace logging (level 5)
	LevelVerbose = 5
)

// WithLevel returns a logger with the specified verbosity level
func WithLevel(logger logr.Logger, level int) logr.Logger {
	return logger.V(level)
}

// Error logs an error message (always shown)
func Error(logger logr.Logger, err error, msg string, keysAndValues ...any) {
	logger.Error(err, msg, keysAndValues...)
}

// Warning logs a warning message
func Warning(logger logr.Logger, msg string, keysAndValues ...any) {
	logger.V(LevelWarning).Info(msg, keysAndValues...)
}

// Info logs an info message
func Info(logger logr.Logger, msg string, keysAndValues ...any) {
	logger.V(LevelInfo).Info(msg, keysAndValues...)
}

// Debug logs a debug message
func Debug(logger logr.Logger, msg string, keysAndValues ...any) {
	logger.V(LevelDebug).Info(msg, keysAndValues...)
}

// Trace logs a trace message
func Trace(logger logr.Logger, msg string, keysAndValues ...any) {
	logger.V(LevelTrace).Info(msg, keysAndValues...)
}

// Verbose logs a very verbose trace message
func Verbose(logger logr.Logger, msg string, keysAndValues ...any) {
	logger.V(LevelVerbose).Info(msg, keysAndValues...)
}
