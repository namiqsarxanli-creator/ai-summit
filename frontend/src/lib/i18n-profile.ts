// Texts of the attendee's own profile screen: editing, the photo, sponsor
// consent, conflicts. English is the key (see i18n.ts); each entry needs both
// translations - `PROFILE_TEXTS` lists every text the screen can show so a test
// can guard against a missing one.

export const profileTranslations: Record<string, { az: string; ru: string }> = {
  "Edit profile": { az: "Profili redaktə et", ru: "Изменить профиль" },
  "Save changes": { az: "Dəyişiklikləri saxla", ru: "Сохранить изменения" },
  "Unsaved changes": { az: "Saxlanılmamış dəyişikliklər", ru: "Несохранённые изменения" },
  "No changes yet": { az: "Hələ dəyişiklik yoxdur", ru: "Изменений пока нет" },
  "Profile saved.": { az: "Profil saxlanıldı.", ru: "Профиль сохранён." },
  "Discard your changes?": { az: "Dəyişikliklər ləğv edilsin?", ru: "Отменить изменения?" },
  "Discard changes": { az: "Dəyişiklikləri ləğv et", ru: "Отменить изменения" },
  "Keep editing": { az: "Redaktəyə davam et", ru: "Продолжить редактирование" },
  "Your email can't be changed here.": { az: "E-poçt ünvanı burada dəyişdirilə bilməz.", ru: "Адрес почты здесь изменить нельзя." },
  "Some of your interests are no longer available. Remove them and save again.": { az: "Bəzi maraqlarınız artıq mövcud deyil. Onları silib yenidən saxlayın.", ru: "Некоторые ваши интересы больше недоступны. Уберите их и сохраните снова." },
  "No longer available": { az: "Artıq mövcud deyil", ru: "Больше недоступно" },
  "Your profile was changed on another device": { az: "Profiliniz başqa cihazda dəyişdirilib", ru: "Ваш профиль изменён на другом устройстве" },
  "It was saved after you opened it here — probably by you, on another device. Nothing you typed was lost: your changes are still in the form.": { az: "Profil siz burada açdıqdan sonra saxlanılıb — çox güman ki, başqa cihazda özünüz saxlamısınız. Yazdıqlarınız itməyib: dəyişikliklər hələ də formadadır.", ru: "Профиль был сохранён после того, как вы открыли его здесь, вероятно вами на другом устройстве. Ничего из набранного не потеряно: ваши изменения остались в форме." },
  "Your version": { az: "Sizin variant", ru: "Ваш вариант" },
  "Saved version": { az: "Saxlanılmış variant", ru: "Сохранённая версия" },
  "Save my changes anyway": { az: "Dəyişikliklərimi yenə də saxla", ru: "Всё равно сохранить мои изменения" },
  "Use the saved version": { az: "Saxlanılmış variantı götür", ru: "Взять сохранённую версию" },
  "Not set": { az: "Göstərilməyib", ru: "Не указано" },
  "Profile photo": { az: "Profil şəkli", ru: "Фото профиля" },
  "Add a photo": { az: "Şəkil əlavə et", ru: "Добавить фото" },
  "Change photo": { az: "Şəkli dəyiş", ru: "Изменить фото" },
  "Remove photo": { az: "Şəkli sil", ru: "Удалить фото" },
  "Upload photo": { az: "Şəkli yüklə", ru: "Загрузить фото" },
  "Choose a photo": { az: "Şəkil seçin", ru: "Выберите фото" },
  "Uploading…": { az: "Yüklənir…", ru: "Загружаем…" },
  "Removing…": { az: "Silinir…", ru: "Удаляем…" },
  "Photo updated.": { az: "Şəkil yeniləndi.", ru: "Фото обновлено." },
  "Photo removed.": { az: "Şəkil silindi.", ru: "Фото удалено." },
  "Preview — not uploaded yet": { az: "Önbaxış — hələ yüklənməyib", ru: "Предпросмотр — ещё не загружено" },
  "Shown on the leaderboard and your card. JPEG, PNG or WebP, up to 8 MB.": { az: "Reytinqdə və kartınızda görünür. JPEG, PNG və ya WebP, 8 MB-a qədər.", ru: "Видно в рейтинге и на вашей карточке. JPEG, PNG или WebP, до 8 МБ." },
  "Choose a JPEG, PNG or WebP picture.": { az: "JPEG, PNG və ya WebP şəkli seçin.", ru: "Выберите изображение JPEG, PNG или WebP." },
  "That file is empty. Choose another picture.": { az: "Fayl boşdur. Başqa şəkil seçin.", ru: "Файл пустой. Выберите другое изображение." },
  "That photo is too large. Choose a smaller picture.": { az: "Şəkil çox böyükdür. Daha kiçik şəkil seçin.", ru: "Фото слишком большое. Выберите изображение поменьше." },
  "We can't read this picture. Choose another one.": { az: "Bu şəkli oxumaq olmur. Başqasını seçin.", ru: "Не удаётся прочитать это изображение. Выберите другое." },
  "Share my details with sponsors": { az: "Məlumatlarımı sponsorlarla paylaş", ru: "Делиться моими данными со спонсорами" },
  "Stop sharing my details": { az: "Məlumatlarımın paylaşılmasını dayandır", ru: "Прекратить передачу моих данных" },
  "Saving your choice…": { az: "Seçiminiz saxlanılır…", ru: "Сохраняем ваш выбор…" },
  "Your choice was saved.": { az: "Seçiminiz saxlanıldı.", ru: "Ваш выбор сохранён." },
  "Agreed on": { az: "Razılıq verilib:", ru: "Согласие дано:" },
  "This profile was changed on another device. Review the latest version below.": { az: "Bu profil başqa cihazda dəyişdirilib. Aşağıdakı son variantı nəzərdən keçirin.", ru: "Этот профиль изменён на другом устройстве. Просмотрите последнюю версию ниже." },
  "That file isn't a usable photo. Choose a JPEG, PNG or WebP picture that is not animated.": { az: "Bu fayl yararlı şəkil deyil. Animasiyasız JPEG, PNG və ya WebP şəkli seçin.", ru: "Этот файл не подходит как фото. Выберите неанимированное изображение JPEG, PNG или WebP." },
  "Photos are being processed right now. Please try again in a moment.": { az: "Hazırda şəkillər emal olunur. Bir azdan yenidən cəhd edin.", ru: "Сейчас обрабатываются другие фото. Попробуйте чуть позже." },
  "Photo storage is unavailable right now. Please try again later.": { az: "Şəkil yaddaşı hazırda əlçatmazdır. Daha sonra yenidən cəhd edin.", ru: "Хранилище фото сейчас недоступно. Попробуйте позже." },
};

// Every text this screen shows through `t()`, besides the ones other screens
// already translate (registration's field labels, the consent copy...).
export const PROFILE_TEXTS: string[] = Object.keys(profileTranslations);
