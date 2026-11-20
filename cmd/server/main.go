package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/aivoya/acquisition/internal/httpapi"
	"github.com/aivoya/acquisition/internal/store"
)

func env(name, def string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return def
}
func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	admin := os.Getenv("AIVOYA_ADMIN_TOKEN")
	worker := os.Getenv("AIVOYA_WORKER_TOKEN")
	if len(admin) < 24 || len(worker) < 24 || admin == worker {
		logger.Error("set two distinct AIVOYA_ADMIN_TOKEN / AIVOYA_WORKER_TOKEN of >=24 chars")
		os.Exit(1)
	}
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		logger.Error("DATABASE_URL required")
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	db, e := store.Open(ctx, dsn)
	if e != nil {
		logger.Error("database", "error", e)
		os.Exit(1)
	}
	defer db.DB.Close()
	if e = db.Migrate(ctx); e != nil {
		logger.Error("migrate", "error", e)
		os.Exit(1)
	}
	api := httpapi.Server{Store: db, AdminToken: admin, WorkerToken: worker, Logger: logger}
	srv := &http.Server{Addr: env("ADDR", ":8080"), Handler: api.Routes(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 35 * time.Second, WriteTimeout: 40 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 1 << 20}
	go func() {
		ticker := time.NewTicker(20 * time.Second)
		defer ticker.Stop()
		for {
			if err := db.ReapExpired(ctx); err != nil && ctx.Err() == nil {
				logger.Warn("reap", "error", err)
			}
			if n, err := db.Schedule(ctx); err != nil && ctx.Err() == nil {
				logger.Warn("schedule", "error", err)
			} else if n > 0 {
				logger.Info("scheduled", "jobs", n)
			}
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
	go func() {
		ticker := time.NewTicker(6 * time.Hour)
		defer ticker.Stop()
		for {
			if n, e := db.PurgeRestrictedSnapshots(ctx); e != nil && ctx.Err() == nil {
				logger.Warn("snapshot purge", "error", e)
			} else if n > 0 {
				logger.Info("purged restricted snapshots", "count", n)
			}
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
		}
	}()
	go func() {
		<-ctx.Done()
		c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(c)
	}()
	logger.Info("listening", "addr", srv.Addr, "paths", strings.Join([]string{"/console", "/v1/stats", "/v1/sources", "/v1/jobs"}, ","))
	if e := srv.ListenAndServe(); e != nil && e != http.ErrServerClosed {
		logger.Error("server error", "error", e)
		os.Exit(1)
	}
}
