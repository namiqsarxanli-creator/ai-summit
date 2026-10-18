import { flowTranslations } from "./i18n-flow";
import { pageTranslations } from "./i18n-pages";
import { errorTranslations } from "./i18n-errors";
import { staffTranslations } from "./i18n-staff";
import { adminTranslations } from "./i18n-admin";
import { contentTranslations } from "./i18n-content";
import { profileTranslations } from "./i18n-profile";
import { cardTranslations } from "./i18n-card";
import { competitionTranslations } from "./i18n-competition";
import { finalTranslations } from "./i18n-final";
import { mentorTranslations } from "./i18n-mentors";
import { peopleTranslations } from "./i18n-people";
import { playgroundTranslations } from "./i18n-playground";
import { masterclassTranslations } from "./i18n-masterclass";

export type Language = "az" | "en" | "ru";

export const LANGUAGE_STORAGE_KEY = "facemark.language";

export const languages: { code: Language; label: string }[] = [
  { code: "az", label: "Azərbaycan" },
  { code: "en", label: "English" },
  { code: "ru", label: "Русский" },
];

export function isLanguage(value: string | null | undefined): value is Language {
  return value === "az" || value === "en" || value === "ru";
}

const translations: Record<string, { az: string; ru: string }> = {
  "Remember me": { az: "Yadda saxla", ru: "Запомнить меня" },
  "Scan a QR code to open the attendee card.": { az: "İştirakçı kartını açmaq üçün QR kodu skan edin.", ru: "Отсканируйте QR-код, чтобы открыть карточку участника." },
  "Back to booths": { az: "Stendlərə qayıt", ru: "Назад к стендам" },
  "Collected points": { az: "Topladığınız xallar", ru: "Набранные баллы" },
  "Discover more. Earn more points.": { az: "Daha çox iştirak et, daha çox xal topla.", ru: "Участвуйте больше — получайте больше баллов." },
  "Your summit, your connections.": { az: "Sizin tədbiriniz. Sizin əlaqələriniz.", ru: "Ваше событие. Ваши знакомства." },
  "Language": { az: "Dil", ru: "Язык" },
  "Technology by": { az: "Texnoloji həll:", ru: "Технологическое решение:" },
  "Discover new connections": { az: "Yeni əlaqələr kəşf edin", ru: "Откройте новые знакомства" },
  "Discover booth teams": { az: "Stendləri kəşf edin", ru: "Откройте стенды" },
  "Show your attendee card": { az: "İştirakçı kartınızı göstərin", ru: "Покажите карточку участника" },
  "Discover sponsors and join activities": { az: "Sponsorları kəşf et, fəaliyyətlərə qoşul", ru: "Откройте спонсоров и участвуйте в активностях" },
  "Make event participation easier": { az: "Tədbirdə iştirakını asanlaşdır", ru: "Упростите участие в мероприятии" },
  "Your points": { az: "Sizin xallarınız", ru: "Ваши баллы" },
  "Quick actions": { az: "Sürətli keçidlər", ru: "Быстрые действия" },
  "Offline QR": { az: "Offline QR", ru: "Офлайн QR" },
  "Save an encrypted copy on this device. Open this app once while online, then use your PIN to show the QR when offline.": { az: "Bu cihazda şifrələnmiş nüsxə saxlayın. Tətbiqi bir dəfə onlayn açın, sonra offline olanda QR kodu PIN ilə göstərin.", ru: "Сохраните зашифрованную копию на этом устройстве. Откройте приложение один раз онлайн, затем показывайте QR офлайн с помощью PIN-кода." },
  "The QR can be shown offline, but scanning it still needs a connection to the AI Summit server.": { az: "QR kodu offline göstərmək olar, lakin onu skan edən cihazın AI Summit serverinə bağlantısı lazımdır.", ru: "QR-код можно показать офлайн, но сканирующему устройству нужна связь с сервером AI Summit." },
  "Offline QR saved on this device.": { az: "Offline QR bu cihazda saxlanıldı.", ru: "Офлайн QR сохранён на этом устройстве." },
  "Choose a 6–12 digit PIN": { az: "6–12 rəqəmli PIN seçin", ru: "Выберите PIN из 6–12 цифр" },
  "Repeat PIN": { az: "PIN-i təkrarlayın", ru: "Повторите PIN" },
  "Could not save offline QR. Check the PIN and try again.": { az: "Offline QR saxlanmadı. PIN-i yoxlayıb yenidən cəhd edin.", ru: "Не удалось сохранить офлайн QR. Проверьте PIN и повторите попытку." },
  "Saving…": { az: "Saxlanılır…", ru: "Сохраняем…" },
  "Save offline QR": { az: "Offline QR saxla", ru: "Сохранить офлайн QR" },
  "Remove offline copy": { az: "Offline nüsxəni sil", ru: "Удалить офлайн-копию" },
  "Skip to content": { az: "Məzmuna keç", ru: "Перейти к содержанию" },
  "AI Summit · Connect": { az: "AI Summit · Əlaqə", ru: "AI Summit · Общение" },
  "Your people, your discoveries, your summit. Start here.": { az: "İnsanlarınız, kəşfləriniz, sammitiniz. Buradan başlayın.", ru: "Ваши знакомства, открытия и саммит. Начните здесь." },
  "Home": { az: "Əsas", ru: "Главная" },
  "Booths": { az: "Stendlər", ru: "Стенды" },
  "My QR": { az: "QR kodum", ru: "Мой QR" },
  "Leaderboard": { az: "Reytinq", ru: "Рейтинг" },
  "Profile": { az: "Profil", ru: "Профиль" },
  "Attendee navigation": { az: "İştirakçı naviqasiyası", ru: "Навигация участника" },
  "Checking your session…": { az: "Sessiyanız yoxlanılır…", ru: "Проверяем вашу сессию…" },
  "Signed in": { az: "Daxil olmusunuz", ru: "Вы вошли" },
  "Signing out…": { az: "Çıxış edilir…", ru: "Выход…" },
  "Sign out": { az: "Çıxış", ru: "Выйти" },
  "Register as an attendee": { az: "İştirakçı kimi qeydiyyat", ru: "Регистрация участника" },
  "Already registered? Sign in": { az: "Artıq qeydiyyatdan keçmisiniz? Daxil olun", ru: "Уже зарегистрированы? Войти" },
  "AI Summit · Attendee registration": { az: "AI Summit · İştirakçı qeydiyyatı", ru: "AI Summit · Регистрация участника" },
  "Join the summit network": { az: "Sammit şəbəkəsinə qoşulun", ru: "Присоединяйтесь к сообществу саммита" },
  "Register once to get your networking card and personalised booth recommendations. We'll email you a code to confirm it's really you.": { az: "Əlaqə kartınızı və sizə uyğun stend tövsiyələrini almaq üçün qeydiyyatdan keçin. Şəxsiyyətinizi təsdiqləmək üçün sizə kod veriləcək.", ru: "Зарегистрируйтесь, чтобы получить контактную карточку и персональные рекомендации стендов. Для подтверждения личности вам понадобится код." },
  "Already registered?": { az: "Artıq qeydiyyatdan keçmisiniz?", ru: "Уже зарегистрированы?" },
  "Sign in": { az: "Daxil olun", ru: "Войти" },
  "AI Summit · Step 2 of 2": { az: "AI Summit · 2-ci addım", ru: "AI Summit · Шаг 2 из 2" },
  "Check your email": { az: "Təsdiq kodunu yoxlayın", ru: "Проверьте код подтверждения" },
  "Enter the 6-digit code we sent to activate your attendee account.": { az: "İştirakçı hesabınızı aktivləşdirmək üçün 6 rəqəmli kodu daxil edin.", ru: "Введите шестизначный код, чтобы активировать учётную запись участника." },
  "AI Summit · Welcome back": { az: "AI Summit · Yenidən xoş gəlmisiniz", ru: "AI Summit · С возвращением" },
  "No password needed — we'll email you a one-time code.": { az: "Şifrə lazım deyil — birdəfəlik kodla daxil ola bilərsiniz.", ru: "Пароль не нужен — войдите с помощью одноразового кода." },
  "AI Summit · Booths": { az: "AI Summit · Stendlər", ru: "AI Summit · Стенды" },
  "Your booth matches": { az: "Sizə uyğun stendlər", ru: "Подходящие вам стенды" },
  "A deterministic score computed from your selected interests — never randomised, never generated.": { az: "Uyğunluq seçdiyiniz maraqlara əsasən hesablanır; təsadüfi deyil.", ru: "Совпадение рассчитывается по выбранным интересам, а не случайным образом." },
  "← Home": { az: "← Əsas", ru: "← Главная" },
  "AI Summit · Leaderboard": { az: "AI Summit · Reytinq", ru: "AI Summit · Рейтинг" },
  "AI Summit · My card": { az: "AI Summit · Kartım", ru: "AI Summit · Моя карточка" },
  "Your networking QR": { az: "Əlaqə QR kodunuz", ru: "Ваш контактный QR-код" },
  "AI Summit · Profile": { az: "AI Summit · Profil", ru: "AI Summit · Профиль" },
  "Your profile": { az: "Profiliniz", ru: "Ваш профиль" },
  "AI Summit · Staff panel": { az: "AI Summit · Heyət paneli", ru: "AI Summit · Панель персонала" },
  "Your booth": { az: "Stendiniz", ru: "Ваш стенд" },
  "AI Summit · Staff": { az: "AI Summit · Heyət", ru: "AI Summit · Персонал" },
  "Staff sign in": { az: "Heyət girişi", ru: "Вход для персонала" },
  "Accounts are created by an admin — there is no staff self-registration.": { az: "Heyət hesablarını administrator yaradır; ayrıca qeydiyyat yoxdur.", ru: "Учётные записи персонала создаёт администратор; самостоятельной регистрации нет." },
  "Scan attendee": { az: "İştirakçını skan edin", ru: "Сканировать участника" },
  "Back": { az: "Geri", ru: "Назад" },
  ...flowTranslations,
  ...pageTranslations,
  ...errorTranslations,
  ...staffTranslations,
  ...adminTranslations,
  ...contentTranslations,
  ...profileTranslations,
  ...cardTranslations,
  ...competitionTranslations,
  ...finalTranslations,
  ...mentorTranslations,
  ...peopleTranslations,
  ...masterclassTranslations,
  ...playgroundTranslations,
};

// Whether a UI text has an entry (Azerbaijani and Russian) - for tests that guard
// a screen's texts against a missing translation. English is the key itself.
export function hasTranslation(text: string): boolean {
  return text in translations;
}

export function translate(text: string, language: Language): string {
  return language === "en" ? text : (translations[text]?.[language] ?? text);
}

const TITLE_SUFFIX = " · AI Summit";

/** A document title as the server wrote it (English, "<page> · AI Summit")
 * in the visitor's language. The brand is never translated. */
export function localiseTitle(title: string, language: Language): string {
  if (title.endsWith(TITLE_SUFFIX)) {
    return translate(title.slice(0, -TITLE_SUFFIX.length), language) + TITLE_SUFFIX;
  }
  return translate(title, language);
}
