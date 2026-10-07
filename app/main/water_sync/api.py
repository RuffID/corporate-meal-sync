"""Клиент iikoCard API: явные контракты, таймауты и безопасные ошибки."""

from datetime import timedelta
from uuid import UUID

import requests

from .errors import ApiError, SecretFilter, SyncError


class IikoClient:
    def __init__(self, settings, session=None):
        if not settings.api_login or not settings.api_password:
            raise SyncError("Заполните api_login и api_password в локальных настройках.")
        self.settings = settings
        self.session = session if session is not None else requests.Session()
        self.token = None
        self.secrets = SecretFilter(settings.api_password, settings.sftp_password)

    def close(self):
        self.session.close()

    def authenticate(self):
        response = self._request("GET", "/api/0/auth/access_token", authenticated=False,
                                 params={"user_id": self.settings.api_login, "user_secret": self.settings.api_password})
        try:
            token = response.json()
        except ValueError:
            # Даже некорректный ответ авторизации может содержать токен.
            # Его тело не попадает в исключение или журнал.
            raise SyncError("Ответ авторизации не является JSON-строкой токена; содержимое скрыто.") from None
        if not isinstance(token, str) or not token.strip():
            raise SyncError("iikoCard вернул пустой токен или ответ неверного типа.")
        self.token = token.strip()
        self.secrets.add(self.token)

    def _request(self, method, path, params=None, payload=None, authenticated=True):
        query = dict(params or {})
        if authenticated:
            if not self.token:
                raise SyncError("Перед запросами iikoCard необходимо получить токен.")
            query["access_token"] = self.token
        try:
            # POST не повторяется автоматически: результат начисления при обрыве неизвестен.
            response = self.session.request(method, self.settings.api_url + path, params=query,
                                            json=payload, timeout=self.settings.timeout_seconds,
                                            allow_redirects=False)
        except requests.RequestException as error:
            message = self.secrets.clean(str(error))
            raise ApiError(None, None, f"Сетевой сбой {method} {path}: {message}", "") from None
        if response.status_code != 200:
            body = self.secrets.clean(response.text)
            try:
                details = response.json()
            except ValueError:
                details = {}
            code = (details.get("errorCode") or details.get("errorType")) if isinstance(details, dict) else None
            message = f"iikoCard: {method} {path}, HTTP {response.status_code}, код {self.secrets.clean(code)}. Ответ: {body}"
            raise ApiError(response.status_code, code, message, body)
        return response

    def _json(self, response, operation):
        try:
            return response.json()
        except ValueError:
            body = self.secrets.clean(response.text)
            raise SyncError(f"iikoCard {operation}: ожидался JSON, получено: {body}.") from None

    def _guest(self, response, operation):
        guest = self._json(response, operation)
        if not isinstance(guest, dict):
            raise SyncError(f"iikoCard {operation}: ожидается объект гостя.")
        try:
            guest["id"] = str(UUID(guest.get("id", "")))
        except (ValueError, TypeError, AttributeError):
            raise SyncError(f"iikoCard {operation}: отсутствует корректный id гостя.") from None
        return guest

    def by_card(self, track):
        path = "/api/0/customers/get_customer_by_card"
        try:
            response = self._request("GET", path, {"organization": self.settings.organization_id, "card": track})
        except ApiError as error:
            # Это реальный код ответа iikoCard. Другие 400/404 не означают отсутствия гостя.
            if error.status == 400 and error.code == "Card_CanNotFindByNumber":
                return None
            raise
        return self._guest(response, path)

    def by_id(self, guest_id):
        path = "/api/0/customers/get_customer_by_id"
        response = self._request("GET", path, {"organization": self.settings.organization_id, "id": guest_id})
        guest = self._guest(response, path)
        if guest["id"] != guest_id:
            raise SyncError(f"iikoCard вернул гостя {guest['id']} вместо запрошенного {guest_id}.")
        return guest

    def create_guest(self, card):
        response = self._request("POST", "/api/0/customers/create_or_update",
                                 {"organization": self.settings.organization_id},
                                 {"accessToken": self.token, "organization": self.settings.organization_id,
                                  "customer": {"name": card.name, "magnetCardTrack": card.track,
                                               "magnetCardNumber": card.track}})
        value = self._json(response, "create_or_update")
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise SyncError("iikoCard create_or_update: ожидался GUID гостя в JSON-строке.") from None

    def add_category(self, guest_id):
        self._request("POST", f"/api/0/customers/{guest_id}/add_category",
                      {"organization": self.settings.organization_id, "categoryId": self.settings.category_id},
                      {"accessToken": self.token, "organization": self.settings.organization_id,
                       "categoryId": self.settings.category_id, "customerId": guest_id})

    def add_nutrition(self, guest_id):
        self._request("POST", f"/api/0/customers/{guest_id}/add_to_nutrition_organization",
                      {"organization": self.settings.organization_id, "corporate_nutrition_id": self.settings.program_id},
                      {"accessToken": self.token, "customerId": guest_id,
                       "organizationId": self.settings.organization_id, "corporateNutritionId": self.settings.program_id})

    def refill(self, guest_id, marker):
        self._request("POST", "/api/0/customers/refill_balance", payload={
            "customerId": guest_id, "organizationId": self.settings.organization_id,
            "walletId": self.settings.wallet_id, "sum": float(self.settings.refill_amount), "comment": marker,
        })

    def _list(self, response, operation):
        data = self._json(response, operation)
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise SyncError(f"iikoCard {operation}: ожидается массив объектов; обработка отменена.")
        return data

    def transactions(self, day, guest_id):
        path = f"/api/0/organization/{self.settings.organization_id}/transactions_report"
        response = self._request("GET", path, {
            "date_from": day.isoformat(), "date_to": (day + timedelta(days=1)).isoformat(), "userId": guest_id,
        })
        return self._list(response, "transactions_report")

    def nutrition_report(self, start, end):
        path = f"/api/0/organization/{self.settings.organization_id}/corporate_nutrition_report"
        response = self._request("GET", path, {
            "corporate_nutrition_id": self.settings.program_id, "date_from": start.isoformat(), "date_to": end.isoformat(),
        })
        return self._list(response, "corporate_nutrition_report")
