#include "currentpotential.h"
#include <Eigen/Dense>
#include "surface.h"

// template<template<class Array> class Surface, class Array>
template<class Array>
void CurrentPotential<Array>::K_impl_helper(Array& data, Array& dg1, Array& dg2, Array& normal) {
  auto dphid1 = this->Phidash1();
  auto dphid2 = this->Phidash2();
  // auto dg1 = this->winding_surface->gammadash1();
  // auto dg2 = this->winding_surface->gammadash2();
  // auto normal = this->winding_surface->normal();
  // K = n \times Phi
  // N \times \nabla \theta = - dr/dzeta
  // N \times \nabla \zeta = dr/dtheta
  // K = (- dPhidtheta dr/dzeta + dPhidzeta dr/dtheta)/N
  for (int i = 0; i < numquadpoints_phi; ++i) {
      for (int j = 0; j < numquadpoints_theta; ++j) {
          double normn = std::sqrt(normal(i, j, 0)*normal(i, j, 0) + normal(i, j, 1)*normal(i, j, 1) + normal(i, j, 2)*normal(i, j, 2));
          data(i, j, 0) = (- dg1(i,j,0) * (dphid2(i,j) + this->net_toroidal_current_amperes) + dg2(i,j,0) * (dphid1(i,j) + this->net_poloidal_current_amperes))/normn;
          data(i, j, 1) = (- dg1(i,j,1) * (dphid2(i,j) + this->net_toroidal_current_amperes) + dg2(i,j,1) * (dphid1(i,j) + this->net_poloidal_current_amperes))/normn;
          data(i, j, 2) = (- dg1(i,j,2) * (dphid2(i,j) + this->net_toroidal_current_amperes) + dg2(i,j,2) * (dphid1(i,j) + this->net_poloidal_current_amperes))/normn;
      }
  }
}

#include "xtensor-python/pyarray.hpp"     // Numpy bindings
typedef xt::pyarray<double> Array;
template class CurrentPotential<Array>;