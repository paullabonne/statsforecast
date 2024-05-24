# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/src/mfles.ipynb.

# %% auto 0
__all__ = ['MFLES']

# %% ../nbs/src/mfles.ipynb 3
import itertools
import warnings

import numpy as np
from coreforecast.exponentially_weighted import exponentially_weighted_mean
from coreforecast.rolling import rolling_mean
from numba import njit

from .utils import _ensure_float

# %% ../nbs/src/mfles.ipynb 4
# utility functions
def calc_mse(y_true, y_pred):
    sq_err = (y_true - y_pred) ** 2
    return np.mean(sq_err)


def calc_mae(y_true, y_pred):
    abs_err = np.abs(y_true - y_pred)
    return np.mean(abs_err)


def calc_mape(y_true, y_pred):
    pct_err = np.abs((y_true - y_pred) / (y_pred + 1e-6))
    return np.mean(pct_err)


def calc_smape(y_true, y_pred):
    pct_err = 2 * np.abs(y_true - y_pred) / np.abs(y_true + y_pred + 1e-6)
    return np.mean(pct_err)


_metric2fn = {
    "mse": calc_mse,
    "mae": calc_mae,
    "mape": calc_mape,
    "smape": calc_smape,
}


def cross_validation(
    y, X, test_size, n_splits, model_obj, metric, step_size=1, **kwargs
):
    metrics = []
    metric_fn = _metric2fn[metric]
    residuals = []
    if X is None:
        exogenous = None
    else:
        exogenous = X.copy()
    for split in range(n_splits):
        train_y = y[: -(split * step_size + test_size)]
        test_y = y[len(train_y) : len(train_y) + test_size]
        if exogenous is not None:
            train_X = exogenous[: -(split * step_size + test_size), :]
            test_X = exogenous[len(train_y) : len(train_y) + test_size, :]
        else:
            train_X = None
            test_X = None
        model_obj.fit(train_y, X=train_X, **kwargs)
        prediction = model_obj.predict(test_size, X=test_X)
        metrics.append(metric_fn(test_y, prediction))
        residuals.append(test_y - prediction)
    return {"metric": np.mean(metrics), "residuals": residuals}


def logic_check(keys_to_check, keys):
    return set(keys_to_check).issubset(keys)


def logic_layer(param_dict):
    keys = param_dict.keys()
    # if param_dict['n_changepoints'] is None:
    #     if param_dict['decay'] != -1:
    #         return False
    if logic_check(["seasonal_period", "max_rounds"], keys):
        if param_dict["seasonal_period"] is None:
            if param_dict["max_rounds"] < 4:
                return False
    if logic_check(["smoother", "ma"], keys):
        if param_dict["smoother"]:
            if param_dict["ma"] is not None:
                return False
    if logic_check(["seasonal_period", "seasonality_weights"], keys):
        if param_dict["seasonality_weights"]:
            if param_dict["seasonal_period"] is None:
                return False
    return True


def default_configs(seasonal_period, configs=None):
    if configs is None:
        if seasonal_period is not None:
            if not isinstance(seasonal_period, list):
                seasonal_period = [seasonal_period]
            configs = {
                "seasonality_weights": [True, False],
                "smoother": [True, False],
                "ma": [int(min(seasonal_period)), int(min(seasonal_period) / 2), None],
                "seasonal_period": [None, seasonal_period],
            }
        else:
            configs = {
                "smoother": [True, False],
                "cov_threshold": [0.5, -1],
                "max_rounds": [5, 20],
                "seasonal_period": [None],
            }
    keys = configs.keys()
    combinations = itertools.product(*configs.values())
    ds = [dict(zip(keys, cc)) for cc in combinations]
    ds = [i for i in ds if logic_layer(i)]
    return ds


def cap_outliers(series, outlier_cap=3):
    mean = np.mean(series)
    std = np.std(series)
    return series.clip(min=mean - outlier_cap * std, max=mean + outlier_cap * std)


def set_fourier(period):
    if period < 10:
        fourier = 5
    elif period < 70:
        fourier = 10
    else:
        fourier = 15
    return fourier


def calc_trend_strength(resids, deseasonalized):
    return max(0, 1 - (np.var(resids) / np.var(deseasonalized)))


def calc_seas_strength(resids, detrended):
    return max(0, 1 - (np.var(resids) / np.var(detrended)))


def calc_rsq(y, fitted):
    try:
        mean_y = np.mean(y)
        ssres = np.sum((y - fitted) ** 2)
        sstot = np.sum((y - mean_y) ** 2)
        return 1 - (ssres / sstot)
    except:
        return 0


def calc_cov(y, mult=1):
    if mult:
        # source http://medcraveonline.com/MOJPB/MOJPB-06-00200.pdf
        res = np.sqrt(np.exp(np.log(10) * (np.std(y) ** 2) - 1))
    else:
        res = np.std(y)
        mean = np.mean(y)
        if mean != 0:
            res = res / mean
    return res


def get_seasonality_weights(y, seasonal_period):
    return 1 + np.arange(y.size) // seasonal_period


# feature engineering functions
def get_fourier_series(length, seasonal_period, fourier_order):
    x = 2 * np.pi * np.arange(1, fourier_order + 1) / seasonal_period
    t = np.arange(1, length + 1).reshape(-1, 1)
    x = x * t
    return np.hstack([np.cos(x), np.sin(x)])


@njit
def get_basis(y, n_changepoints, decay=-1, gradient_strategy=0):
    if n_changepoints < 1:
        return np.arange(y.size, dtype=np.float64).reshape(-1, 1)
    y = y.copy()
    y -= y[0]
    n = len(y)
    if gradient_strategy:
        gradients = np.abs(y[:-1] - y[1:])
    initial_point = y[0]
    final_point = y[-1]
    mean_y = np.mean(y)
    changepoints = np.empty(shape=(len(y), n_changepoints + 1))
    array_splits = []
    for i in range(1, n_changepoints + 1):
        i = n_changepoints - i + 1
        if gradient_strategy:
            cps = np.argsort(-gradients)
            cps = cps[cps > 0.1 * len(gradients)]
            cps = cps[cps < 0.9 * len(gradients)]
            split_point = cps[i - 1]
            array_splits.append(y[:split_point])
        else:
            split_point = len(y) // i
            array_splits.append(y[:split_point])
            y = y[split_point:]
    len_splits = 0
    for i in range(n_changepoints):
        if gradient_strategy:
            len_splits = len(array_splits[i])
        else:
            len_splits += len(array_splits[i])
        moving_point = array_splits[i][-1]
        left_basis = np.linspace(initial_point, moving_point, len_splits)
        if decay is None:
            end_point = final_point
        else:
            if decay == -1:
                dd = moving_point**2
                if mean_y != 0:
                    dd /= mean_y**2
                if dd > 0.99:
                    dd = 0.99
                if dd < 0.001:
                    dd = 0.001
                end_point = moving_point - ((moving_point - final_point) * (1 - dd))
            else:
                end_point = moving_point - ((moving_point - final_point) * (1 - decay))
        right_basis = np.linspace(moving_point, end_point, n - len_splits + 1)
        changepoints[:, i] = np.append(left_basis, right_basis[1:])
    changepoints[:, i + 1] = np.ones(n)
    return changepoints


def get_future_basis(basis_functions, forecast_horizon):
    n_components = np.shape(basis_functions)[1]
    slopes = np.gradient(basis_functions)[0][-1, :]
    future_basis = np.arange(0, forecast_horizon + 1)
    future_basis += len(basis_functions)
    future_basis = np.transpose([future_basis] * n_components)
    future_basis = future_basis * slopes
    future_basis = future_basis + (basis_functions[-1, :] - future_basis[0, :])
    return future_basis[1:, :]


def lasso_nb(X, y, alpha, tol=0.001, maxiter=10000):
    from sklearn.linear_model import Lasso
    from sklearn.exceptions import ConvergenceWarning

    with warnings.catch_warnings(record=False):
        warnings.filterwarnings("ignore", category=ConvergenceWarning)
        lasso = Lasso(alpha=alpha, fit_intercept=False, tol=tol, max_iter=maxiter)
        lasso.fit(X, y)
    return lasso.coef_


# different models
@njit
def siegel_repeated_medians(x, y):
    # Siegel repeated medians regression
    n = y.size
    slopes = np.empty_like(y)
    slopes_sub = np.empty(shape=n - 1, dtype=y.dtype)
    for i in range(n):
        k = 0
        for j in range(n):
            if i == j:
                continue
            xd = x[j] - x[i]
            if xd == 0:
                slope = 0
            else:
                slope = (y[j] - y[i]) / xd
            slopes_sub[k] = slope
            k += 1
        slopes[i] = np.median(slopes_sub)
    ints = y - slopes * x
    return x * np.median(slopes) + np.median(ints)


def ses_ensemble(y, min_alpha=0.05, max_alpha=1.0, smooth=False, order=1):
    # bad name but does either a ses ensemble or simple moving average
    if smooth:
        results = np.zeros_like(y)
        alphas = np.arange(min_alpha, max_alpha, 0.05)
        for alpha in alphas:
            results += exponentially_weighted_mean(y, alpha)
        results = results / len(alphas)
    else:
        results = rolling_mean(y, order + 1)
        results[: order + 1] = y[: order + 1]
    return results


def fast_ols(x, y):
    """Simple OLS for two data sets."""
    M = x.size
    x_sum = x.sum()
    y_sum = y.sum()
    x_sq_sum = x @ x
    x_y_sum = x @ y
    slope = (M * x_y_sum - x_sum * y_sum) / (M * x_sq_sum - x_sum**2)
    intercept = (y_sum - slope * x_sum) / M
    return slope * x + intercept


def median(y, seasonal_period):
    if seasonal_period is None:
        return np.full_like(y, np.median(y))
    full_periods, resid = divmod(len(y), seasonal_period)
    period_medians = np.median(
        y[: full_periods * seasonal_period].reshape(full_periods, seasonal_period),
        axis=1,
    )
    medians = np.repeat(period_medians, seasonal_period)
    if resid:
        remainder_median = np.median(y[-seasonal_period:])
        medians = np.append(medians, np.repeat(remainder_median, resid))
    return medians


def ols(X, y):
    coefs = np.linalg.pinv(X.T.dot(X)).dot(X.T.dot(y))
    return X @ coefs


def wls(X, y, weights):
    weighted_X_T = X.T @ np.diag(weights)
    coefs = np.linalg.pinv(weighted_X_T.dot(X)).dot(weighted_X_T.dot(y))
    return X @ coefs


def _ols(X, y):
    return np.linalg.pinv(X.T.dot(X)).dot(X.T.dot(y))


class OLS:
    def fit(self, X, y):
        self.coefs = _ols(X, y)

    def predict(self, X):
        return X @ self.coefs


class Zeros:
    def predict(self, X):
        return np.zeros(X.shape[0])

# %% ../nbs/src/mfles.ipynb 5
class MFLES:
    def __init__(self, verbose=1, robust=None):
        self.penalty = None
        self.trend = None
        self.seasonality = None
        self.robust = robust
        self.const = None
        self.aic = None
        self.upper = None
        self.lower = None
        self.exogenous_models = None
        self.verbose = verbose
        self.predicted = None

    def fit(
        self,
        y,
        seasonal_period=None,
        X=None,
        fourier_order=None,
        ma=None,
        alpha=1.0,
        decay=-1,
        n_changepoints=0.25,
        seasonal_lr=0.9,
        rs_lr=1,
        exogenous_lr=1,
        exogenous_estimator=OLS,
        exogenous_params={},
        linear_lr=0.9,
        cov_threshold=0.7,
        moving_medians=False,
        max_rounds=50,
        min_alpha=0.05,
        max_alpha=1.0,
        round_penalty=0.0001,
        trend_penalty=True,
        multiplicative=None,
        changepoints=True,
        smoother=False,
        seasonality_weights=False,
    ):
        """


        Parameters
        ----------
        y : np.array
            the time series as a numpy array.
        seasonal_period : int, optional
            DESCRIPTION. The default is None.
        fourier_order : int, optional
            How many fourier sin/cos pairs to create, the larger the number the more complex of a seasonal pattern can be fitted. A lower number leads to smoother results. This is auto-set based on seasonal_period. The default is None.
        ma : int, optional
            The moving average order to use, this is auto-set based on internal logic. Passing 4 would fit a 4 period moving average on the residual component. The default is None.
        alpha : TYPE, optional
            The alpha which is used in fitting the underlying LASSO when using piecewise functions. The default is 1.0.
        decay : float, optional
            Effects the slopes of the piecewise-linear basis function. The default is -1.
        n_changepoints : float, optional
            The number of changepoint knots to place, a default of .25 with place .25 * series length number of knots. The default is .25.
        seasonal_lr : float, optional
            A shrinkage parameter (0<seasonal_lr<=1) which penalizes the seasonal fit, a .9 will flatly multiply the seasonal fit by .9 each boosting round, this can be used to allow more signal to the exogenous component. The default is .9.
        rs_lr : float, optional
            A shrinkage parameter (0<rs_lr<=1) which penalizes the residual smoothing, a .9 will flatly multiply the residual fit by .9 each boosting round, this can be used to allow more signal to the seasonality or linear components. The default is 1.
        linear_lr : float, optional
            A shrinkage parameter (0<linear_lr<=1) which penalizes the linear trend fit, a .9 will flatly multiply the linear fit by .9 each boosting round, this can be used to allow more signal to the seasonality or exogenous components. The default is .9.
        cov_threshold : float, optional
            The deseasonalized cov is used to auto-set some logic, lowering the cov_threshold will result in simpler and less complex residual smoothing. If you pass something like 1000 then there will be no safeguards applied. The default is .7.
        moving_medians : boolean, optional
            The default behavior is to fit an initial median to the time series, if you pass True to moving_medians then it will fit a median per seasonal period. The default is False.
        max_rounds : int, optional
            The max number of boosting rounds. The boosting will auto-stop but depending on other parameters such as rs_lr you may want more rounds. Generally, the more rounds => the more smooth your fit. The default is 10.
        min_alpha : float, optional
            The min alpha in the SES ensemble. The default is .05.
        max_alpha : float, optional
            The max alpha used in the SES ensemble. The default is 1.0.
        trend_penalty : boolean, optional
            Whether to apply a simple penalty to the lienar trend component, very useful for dealing with the potentially dangerous piecewise trend. The default is True.
        multiplicative : boolean, optional
            Auto-set based on internal logic, but if given True it will simply take the log of the time series. The default is None.
        changepoints : boolean, optional
            Whether to fit for changepoints if all other logic allows for it, by setting False then MFLES will not ever fit a piecewise trend. The default is True.
        smoother : boolean, optional
            If True then a simple exponential ensemble will be used rather than auto settings. The default is False.

        Returns
        -------
        None.

        """
        if cov_threshold == -1:
            cov_threshold = 10000
        n = len(y)
        y = _ensure_float(y)
        self.exogenous_lr = exogenous_lr
        if multiplicative is None:
            if seasonal_period is None:
                multiplicative = False
            else:
                multiplicative = True
            if y.min() <= 0:
                multiplicative = False
        if multiplicative:
            self.const = y.min()
            y = np.log(y)
        else:
            self.const = None
            self.std = np.std(y)
            self.mean = np.mean(y)
            y = y - self.mean
            if self.std > 0:
                y = y / self.std
        if seasonal_period is not None:
            if not isinstance(seasonal_period, list):
                seasonal_period = [seasonal_period]
        if n < 4 or np.all(y == np.mean(y)):
            if self.verbose:
                if n < 4:
                    print("series is too short (<4), defaulting to naive")
                else:
                    print(f"input is constant with value {y[0]}, defaulting to naive")
            self.trend = np.append(y[-1], y[-1])
            self.seasonality = np.zeros(len(y))
            self.trend_penalty = False
            self.mean = y[-1]
            self.std = 0
            self.exo_model = [Zeros()]
            return np.tile(y[-1], len(y))
        og_y = y
        self.og_y = og_y
        y = y.copy()
        if n_changepoints is None:
            changepoints = False
        if isinstance(n_changepoints, float) and n_changepoints < 1:
            n_changepoints = int(n_changepoints * n)
        self.linear_component = np.zeros(n)
        self.seasonal_component = np.zeros(n)
        self.ses_component = np.zeros(n)
        self.median_component = np.zeros(n)
        self.exogenous_component = np.zeros(n)
        self.exo_model = []
        self.round_cost = []
        self.trend_penalty = trend_penalty
        if moving_medians and seasonal_period is not None:
            fitted = median(y, max(seasonal_period))
        else:
            fitted = median(y, None)
        self.median_component += fitted
        self.trend = np.append(fitted.copy()[-1:], fitted.copy()[-1:])
        mse = None
        equal = 0
        if ma is None:
            ma_cycle = itertools.cycle([1])
        else:
            if not isinstance(ma, list):
                ma = [ma]
            ma_cycle = itertools.cycle(ma)
        if seasonal_period is not None:
            seasons_cycle = itertools.cycle(list(range(len(seasonal_period))))
            self.seasonality = np.zeros(max(seasonal_period))
            fourier_series = []
            for period in seasonal_period:
                if fourier_order is None:
                    fourier = set_fourier(period)
                else:
                    fourier = fourier_order
                fourier_series.append(get_fourier_series(n, period, fourier))
            if seasonality_weights:
                cycle_weights = []
                for period in seasonal_period:
                    cycle_weights.append(get_seasonality_weights(y, period))
        else:
            self.seasonality = None
        for i in range(max_rounds):
            resids = y - fitted
            if mse is None:
                mse = calc_mse(y, fitted)
            else:
                if mse <= calc_mse(y, fitted):
                    if equal == 6:
                        break
                    equal += 1
                else:
                    mse = calc_mse(y, fitted)
                self.round_cost.append(mse)
            if seasonal_period is not None:
                seasonal_period_cycle = next(seasons_cycle)
                if seasonality_weights:
                    seas = wls(
                        fourier_series[seasonal_period_cycle],
                        resids,
                        cycle_weights[seasonal_period_cycle],
                    )
                else:
                    seas = ols(fourier_series[seasonal_period_cycle], resids)
                seas = seas * seasonal_lr
                component_mse = calc_mse(y, fitted + seas)
                if mse > component_mse:
                    mse = component_mse
                    fitted += seas
                    resids = y - fitted
                    self.seasonality += np.resize(
                        seas[-seasonal_period[seasonal_period_cycle] :],
                        len(self.seasonality),
                    )
                    self.seasonal_component += seas
            if X is not None and i > 0:
                model_obj = exogenous_estimator(**exogenous_params)
                model_obj.fit(X, resids)
                self.exo_model.append(model_obj)
                _fitted_values = model_obj.predict(X) * exogenous_lr
                self.exogenous_component += _fitted_values
                fitted += _fitted_values
                resids = y - fitted
            if (
                i % 2
            ):  # if even get linear piece, allows for multiple seasonality fitting a bit more
                if self.robust:
                    tren = siegel_repeated_medians(
                        x=np.arange(n, dtype=resids.dtype), y=resids
                    )
                else:
                    if i == 1 or not changepoints:
                        tren = fast_ols(x=np.arange(n), y=resids)
                    else:
                        cps = min(n_changepoints, int(0.1 * n))
                        lbf = get_basis(y=resids, n_changepoints=cps, decay=decay)
                        tren = np.dot(lbf, lasso_nb(lbf, resids, alpha=alpha))
                        tren = tren * linear_lr
                component_mse = calc_mse(y, fitted + tren)
                if mse > component_mse:
                    mse = component_mse
                    fitted += tren
                    self.linear_component += tren
                    self.trend += tren[-2:]
                    if i == 1:
                        self.penalty = calc_rsq(resids, tren)
            elif i > 4 and not i % 2:
                if smoother is None:
                    if seasonal_period is not None:
                        len_check = int(max(seasonal_period))
                    else:
                        len_check = 12
                    if resids[-1] > np.mean(resids[-len_check:-1]) + 3 * np.std(
                        resids[-len_check:-1]
                    ):
                        smoother = 0
                    if resids[-1] < np.mean(resids[-len_check:-1]) - 3 * np.std(
                        resids[-len_check:-1]
                    ):
                        smoother = 0
                    if resids[-2] > np.mean(resids[-len_check:-2]) + 3 * np.std(
                        resids[-len_check:-2]
                    ):
                        smoother = 0
                    if resids[-2] < np.mean(resids[-len_check:-2]) - 3 * np.std(
                        resids[-len_check:-2]
                    ):
                        smoother = 0
                    if smoother is None:
                        smoother = 1
                    else:
                        resids[-2:] = cap_outliers(resids, 3)[-2:]
                tren = ses_ensemble(
                    resids,
                    min_alpha=min_alpha,
                    max_alpha=max_alpha,
                    smooth=smoother * 1,
                    order=next(ma_cycle),
                )
                tren = tren * rs_lr
                component_mse = calc_mse(y, fitted + tren)
                if mse > component_mse + round_penalty * mse:
                    mse = component_mse
                    fitted += tren
                    self.ses_component += tren
                    self.trend += tren[-1]
            if i == 0:  # get deasonalized cov for some heuristic logic
                if self.robust is None:
                    try:
                        if calc_cov(resids, multiplicative) > cov_threshold:
                            self.robust = True
                        else:
                            self.robust = False
                    except:
                        self.robust = True

            if i == 1:
                resids = cap_outliers(
                    resids, 5
                )  # cap extreme outliers after initial rounds
        if multiplicative:
            fitted = np.exp(fitted)
        else:
            fitted = self.mean + (fitted * self.std)
        self.multiplicative = multiplicative
        return fitted

    def predict(self, forecast_horizon, X=None):
        last_point = self.trend[1]
        slope = last_point - self.trend[0]
        if self.trend_penalty and self.penalty is not None:
            slope = slope * max(0, self.penalty)
        self.predicted_trend = slope * np.arange(1, forecast_horizon + 1) + last_point
        if self.seasonality is not None:
            predicted = self.predicted_trend + np.resize(
                self.seasonality, forecast_horizon
            )
        else:
            predicted = self.predicted_trend
        if X is not None:
            for model in self.exo_model:
                predicted += model.predict(X) * self.exogenous_lr
        if self.const is not None:
            predicted = np.exp(predicted)
        else:
            predicted = self.mean + (predicted * self.std)
        return predicted

    def optimize(
        self,
        y,
        test_size,
        n_steps,
        step_size=1,
        seasonal_period=None,
        metric="smape",
        X=None,
        params=None,
    ):
        """
        Optimization method for MFLES

        Parameters
        ----------
        y : np.array
            Your time series as a numpy array.
        test_size : int
            length of the test set to hold out to calculate test error.
        n_steps : int
            number of train and test sets to create.
        step_size : 1, optional
            how many periods to move after each step. The default is 1.
        seasonal_period : int or list, optional
            the seasonal period to calculate for. The default is None.
        metric : TYPE, optional
            supported metrics are smape, mape, mse, mae. The default is 'smape'.
        params : dict, optional
            A user provided dictionary of params to try. The default is None.

        Returns
        -------
        opt_param : TYPE
            DESCRIPTION.

        """
        configs = default_configs(seasonal_period, params)
        # the 4 here is because with less than 4 samples the model defaults to naive
        max_steps = (len(y) - test_size - 4) // step_size + 1
        if max_steps < 1:
            if self.verbose:
                print(
                    "Series does not have enough samples for a single cross validation step "
                    f"({test_size + 4}). Choosing the first configuration."
                )
            return configs[0]
        if max_steps < n_steps:
            n_steps = max_steps
            if self.verbose:
                print(f"Series length too small, setting n_steps to {n_steps}")

        self.metrics = []
        for param in configs:
            cv_results = cross_validation(
                y,
                X,
                test_size,
                n_steps,
                MFLES(verbose=self.verbose),
                step_size=step_size,
                metric=metric,
                **param,
            )
            self.metrics.append(cv_results["metric"])
        return configs[np.argmin(self.metrics)]

    def seasonal_decompose(self, y, **kwargs):
        fitted = self.fit(y, **kwargs)
        trend = self.linear_component
        exogenous = self.median_component + self.exogenous_component
        level = self.median_component + self.ses_component
        seasonality = self.seasonal_component
        if self.multiplicative:
            trend = np.exp(trend)
            level = np.exp(level)
            exogenous = np.exp(exogenous) - np.exp(self.median_component)
            if kwargs["seasonal_period"] is not None:
                seasonality = np.exp(seasonality)
            trend = trend * level
        else:
            trend = self.mean + (trend * self.std)
            level = self.mean + (level * self.std)
            exogenous = self.mean + (exogenous * self.std)
            if kwargs["seasonal_period"] is not None:
                seasonality = seasonality * self.std
            trend = trend + level - self.mean
        residuals = y - fitted
        self.decomposition = {
            "y": y,
            "trend": trend,
            "seasonality": seasonality,
            "exogenous": exogenous,
            "residuals": residuals,
        }
        return self.decomposition