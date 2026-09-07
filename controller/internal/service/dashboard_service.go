/*
Copyright 2024.

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

package service

import (
	"context"
	"embed"
	"html/template"
	"net/http"

	"github.com/gin-gonic/gin"
	jumpstarterdevv1alpha1 "github.com/jumpstarter-dev/jumpstarter/controller/api/v1alpha1"

	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

//go:embed templates/*
var fs embed.FS

type DashboardService struct {
	client.Client
	Scheme *runtime.Scheme
}

func (s *DashboardService) Start(ctx context.Context) error {
	r := gin.Default()

	r.SetHTMLTemplate(template.Must(template.ParseFS(fs, "templates/*")))

	r.GET("/", func(c *gin.Context) {
		var exporters jumpstarterdevv1alpha1.ExporterList
		if err := s.List(ctx, &exporters); err != nil {
			c.String(http.StatusInternalServerError, err.Error())
			return
		}

		var clients jumpstarterdevv1alpha1.ClientList
		if err := s.List(ctx, &clients); err != nil {
			c.String(http.StatusInternalServerError, err.Error())
			return
		}

		var leases jumpstarterdevv1alpha1.LeaseList
		if err := s.List(ctx, &leases); err != nil {
			c.String(http.StatusInternalServerError, err.Error())
			return
		}

		c.HTML(http.StatusOK, "index.html", map[string]any{
			"Exporters": exporters.Items,
			"Clients":   clients.Items,
			"Leases":    leases.Items,
		})
	})

	return r.Run(":8084")
}

func (s *DashboardService) NeedLeaderElection() bool {
	return false
}

// SetupWithManager sets up the controller with the Manager.
func (s *DashboardService) SetupWithManager(mgr ctrl.Manager) error {
	return mgr.Add(s)
}
